import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
import uuid
from tools import manage

SOURCE = Path(__file__).resolve().parents[1]

class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mvp tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copy2(SOURCE / ".env.example", self.root / ".env.example")
        patcher = patch.object(manage, "ROOT", self.root)
        patcher.start(); self.addCleanup(patcher.stop)
        (self.root / ".state").mkdir()
        manage.init()
        self.config = manage.validate(manage.parse_env(self.root / ".env"))

    def test_init_generates_secrets(self):
        for name in manage.SECRET_KEYS:
            self.assertEqual(len(self.config[name]), 48)
        self.assertEqual(len(base64.urlsafe_b64decode(self.config["KRAFT_CLUSTER_ID"] + "==")), 16)
        self.assertEqual((self.root / ".env").stat().st_mode & 0o777, 0o600)

    def test_init_preserves_existing_bytes(self):
        before = (self.root / ".env").read_bytes()
        manage.init()
        self.assertEqual((self.root / ".env").read_bytes(), before)

    def test_literal_env_refuses_command_substitution(self):
        path = self.root / "bad.env"
        path.write_text('KEY=$(touch SHOULD_NOT_EXIST)\n')
        with self.assertRaises(ValueError): manage.parse_env(path)
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_literal_env_refuses_duplicate_keys(self):
        path = self.root / "bad.env"; path.write_text("KEY=a\nKEY=b\n")
        with self.assertRaises(ValueError): manage.parse_env(path)

    def test_project_scope_restricted(self):
        for name in ["prod", "db-lab", "../../lab", "-bad"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                manage.validate(dict(self.config, MVP_PROJECT=name))

    def test_invalid_ports_password_and_cluster_id(self):
        for changes in [{"API_PORT": "22"}, {"SQL_PASSWORD": "change-me"},
                        {"KRAFT_CLUSTER_ID": str(uuid.uuid4())}, {"CACHE_TTL": "9999"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                manage.validate(dict(self.config, **changes))

    def compose(self):
        with patch.object(manage, "provider", return_value=(["fake-compose"], ["fake-engine"])):
            return manage.Compose(self.config)

    def test_identity_changes_are_blocked(self):
        compose = self.compose(); compose.guard_identity()
        for key in (*manage.SECRET_KEYS, "KRAFT_CLUSTER_ID", "MVP_PROJECT"):
            compose.config = dict(self.config, **{key: "changed"})
            with self.subTest(key=key), self.assertRaises(RuntimeError): compose.guard_identity()
        self.assertNotIn(self.config["SQL_PASSWORD"], (self.root / ".state/identity.json").read_text())

    def test_shutdown_target_change_is_blocked(self):
        compose = self.compose(); compose.guard_identity()
        compose.config = dict(self.config, MVP_PROJECT="db-lab-mvp-other")
        with self.assertRaises(RuntimeError): compose.guard_target()

    def test_credential_change_does_not_block_same_target_shutdown(self):
        compose = self.compose(); compose.guard_identity()
        compose.config = dict(self.config, SQL_PASSWORD="changed-password-for-stop")
        compose.guard_target()

    def test_image_change_does_not_reset_identity(self):
        compose = self.compose(); compose.guard_identity()
        compose.config = dict(self.config, REDIS_IMAGE="redis:another-tag")
        compose.guard_identity()

    def test_experiment_normal_ignores_inherited_overrides(self):
        with patch.dict(os.environ, {"APP_SQL_PASSWORD": "accidental", "CACHE_HOST": "wrong"}):
            env = manage.environment(self.config, "normal")
        self.assertEqual(env["APP_SQL_PASSWORD"], self.config["SQL_PASSWORD"])
        self.assertEqual(env["CACHE_HOST"], "redis")

    def test_wrong_password_does_not_change_real_sql_password(self):
        env = manage.environment(self.config, "bad-db-password")
        self.assertEqual(env["SQL_PASSWORD"], self.config["SQL_PASSWORD"])
        self.assertNotEqual(env["APP_SQL_PASSWORD"], env["SQL_PASSWORD"])

    def test_spare_and_fresh_search_are_scoped(self):
        env = manage.environment(self.config, "redis-spare")
        self.assertEqual(env["CACHE_HOST"], "redis-spare")
        self.assertEqual(env["SEARCH_INDEX"], "mvp-orders-v1")
        env = manage.environment(self.config, "fresh-search")
        self.assertEqual(env["SEARCH_INDEX"], "mvp-orders-v2")
        self.assertEqual(env["CACHE_HOST"], "redis")

    def test_saved_mode_validated(self):
        manage.atomic_json(self.root / ".state/experiment.json", {"mode": "production"})
        with self.assertRaises(ValueError): manage.mode()

    def test_stop_requires_confirmation_and_scope(self):
        for args in [["stop", "kafka"], ["recreate", "redis"], ["stop", "anything", "--yes"],
                     ["experiment", "redis-spare"], ["rebuild-search"]]:
            with self.subTest(args=args), self.assertRaises(SystemExit): manage.parser().parse_args(args)

    def test_no_reset_or_generic_compose_passthrough(self):
        for args in [["reset", "--yes"], ["compose", "down", "-v"]]:
            with self.assertRaises(SystemExit): manage.parser().parse_args(args)

    def test_compose_args_preserve_paths_with_spaces(self):
        compose = self.compose()
        with patch.object(manage.subprocess, "run") as run:
            compose.run("logs", "api")
        command = run.call_args.args[0]
        self.assertIn(str(self.root / ".env"), command)
        self.assertIn(str(self.root / "compose.yaml"), command)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_lock_rejects_overlapping_operation(self):
        with manage.lock():
            with self.assertRaises(RuntimeError):
                with manage.lock(): pass

    def test_down_does_not_delete_volumes(self):
        with patch.object(manage, "Compose") as cls:
            self.assertEqual(manage.main(["down"]), 0)
        cls.return_value.run.assert_called_once_with("down", profile=True)

    def test_recreate_keeps_volume_and_other_services(self):
        with patch.object(manage, "Compose") as cls:
            self.assertEqual(manage.main(["recreate", "redis", "--yes"]), 0)
        cls.return_value.run.assert_called_once_with("up", "-d", "--no-deps", "--force-recreate", "redis", profile=False)

    def test_wrong_password_up_fails_before_any_up(self):
        manage.atomic_json(self.root / ".state/experiment.json", {"mode": "bad-db-password"})
        with patch.object(manage, "Compose") as cls:
            self.assertEqual(manage.main(["up"]), 1)
        cls.return_value.run.assert_not_called()

    def test_normal_mode_never_restarts_stopped_databases(self):
        with patch.object(manage, "Compose") as cls:
            self.assertEqual(manage.main(["experiment", "normal", "--yes"]), 0)
        cls.return_value.run.assert_called_once_with("up", "-d", "--no-deps", "--force-recreate", "api", "worker")

    def test_sql_rejects_multiple_and_write_statements(self):
        for query in ["DROP TABLE orders", "SELECT 1; DROP TABLE orders", "SELECT 1 INTO OUTFILE '/tmp/x'"]:
            with self.subTest(query=query), patch.object(manage, "Compose") as cls:
                self.assertEqual(manage.main(["sql", query]), 1)
                cls.return_value.run.assert_not_called()

    def test_no_runtime_required_for_help(self):
        with patch.object(manage, "provider", side_effect=AssertionError("must not probe")):
            self.assertEqual(manage.main([]), 0)


class RootRoutingTests(unittest.TestCase):
    def test_dry_run_routes_without_writing(self):
        root = SOURCE.parent
        before = list((SOURCE / ".state").glob("*")) if (SOURCE / ".state").exists() else []
        result = subprocess.run(["bash", str(root / "all.sh"), "--dry-run", "mvp", "up"],
                                cwd="/tmp", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mvp-lab/lab.sh up", result.stdout)
        after = list((SOURCE / ".state").glob("*")) if (SOURCE / ".state").exists() else []
        self.assertEqual(before, after)

    def test_mvp_not_added_to_default_project_list(self):
        text = (SOURCE.parent / "all.sh").read_text()
        self.assertIn("PROJECTS=(elasticsearch kafka mariadb redis)", text)
