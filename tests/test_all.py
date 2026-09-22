#!/usr/bin/env python3
"""Host-only tests for all.sh. No real container, database or Kubernetes calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'all.sh'
BASH = shutil.which('bash')
PROJECT_DIRS = {
    'elasticsearch': 'elasticsearch', 'kafka': 'kafka-lab',
    'mariadb': 'mariadb-ha-lab', 'redis': 'redis-lab',
}
TEST_SCRIPTS = {
    'elasticsearch': 'scripts/12-offline-tests.sh',
    'kafka': 'scripts/test-static.sh',
    'mariadb': 'tests/validate.sh',
    'redis': 'tests/check.sh',
}


class AllControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='db-lab root tests ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'project with spaces'
        self.root.mkdir()
        shutil.copy2(SOURCE, self.root / 'all.sh')
        (self.root / 'scripts').mkdir()
        # Controller tests isolate prerequisite/cluster checks; real guards are tested separately.
        (self.root / 'scripts/control.py').write_text('import sys\n')
        (self.root / 'scripts/k8s_guard.py').write_text('print(\'{"seal_needed": false}\')\n')
        self.log = self.base / 'calls.jsonl'
        # A JSON recorder preserves argument boundaries, including SQL and spaces.
        self.recorder = self.base / 'record.py'
        self.recorder.write_text('''import json, os, sys
from pathlib import Path
name, *args = sys.argv[1:]
with Path(os.environ['ALL_TEST_LOG']).open('a') as stream:
    stream.write(json.dumps({'name': name, 'args': args, 'cwd': os.getcwd(),
                             'leak': os.environ.get('CHILD_ONLY')}) + '\\n')
if name == os.environ.get('ALL_TEST_FAIL') and (not os.environ.get('ALL_TEST_FAIL_CMD') or
                                              (args and args[0] == os.environ['ALL_TEST_FAIL_CMD'])):
    sys.exit(int(os.environ.get('ALL_TEST_FAIL_RC', '7')))
''')
        self.env = dict(os.environ)
        for key in tuple(self.env):
            if key.startswith(('ALL_TEST_', 'DB_LAB_')) or key == 'CHILD_ONLY':
                del self.env[key]
        self.env.update(DB_LAB_CONTEXT='kind-db-lab', ALL_TEST_LOG=str(self.log), ALL_TEST_RECORDER=str(self.recorder),
                        PYTHONDONTWRITEBYTECODE='1')
        for project, dirname in PROJECT_DIRS.items():
            directory = self.root / dirname
            directory.mkdir()
            # Intentionally not executable: all.sh must invoke child scripts via Bash.
            (directory / 'lab.sh').write_text(
                '#!/usr/bin/env bash\nset -euo pipefail\n'
                f'python3 -S "$ALL_TEST_RECORDER" "{project}" "$@"\n'
                'export CHILD_ONLY=must-not-leak\n')
            (directory / '.env.example').write_text('SETTING=original\n')
            test = directory / TEST_SCRIPTS[project]
            test.parent.mkdir(exist_ok=True)
            test.write_text('#!/usr/bin/env bash\n'
                            f'python3 -S "$ALL_TEST_RECORDER" "test:{project}" "$@"\n')
        # es9 (elasticsearch-9) is an opt-in project: it is NOT part of the
        # default batch or the PROJECT_DIRS mapping, but must be selectable.
        self.es9 = self.root / 'elasticsearch-9'
        self.es9.mkdir()
        (self.es9 / 'lab.sh').write_text(
            '#!/usr/bin/env bash\nset -euo pipefail\n'
            'python3 -S "$ALL_TEST_RECORDER" "es9" "$@"\n'
            'export CHILD_ONLY=must-not-leak\n')
        (self.es9 / '.env.example').write_text('SETTING=original\n')
        es9_test = self.es9 / TEST_SCRIPTS['elasticsearch']
        es9_test.parent.mkdir(exist_ok=True)
        es9_test.write_text('#!/usr/bin/env bash\n'
                            'python3 -S "$ALL_TEST_RECORDER" "test:es9" "$@"\n')
        (self.root / 'helmchart').mkdir()
        (self.root / 'helmchart/Chart.yaml').write_text('apiVersion: v2\nname: db-lab\nversion: 0.1.0\n')
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        kubectl = self.bin / 'kubectl'
        kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
        helm = self.bin / 'helm'
        helm.write_text('#!/usr/bin/env bash\nexec python3 -S "$ALL_TEST_RECORDER" helm "$@"\n')
        helm.chmod(0o755)
        self.env['PATH'] = str(self.bin) + os.pathsep + self.env['PATH']

    def run_all(self, *args, code=0, env=None, script=None):
        p = subprocess.run([BASH, str(script or self.root / 'all.sh'), *args],
                           cwd=self.base, env=dict(self.env, **(env or {})),
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, code, p.stdout + '\n' + p.stderr)
        return p

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def names(self):
        return [call['name'] for call in self.calls()]

    def test_no_arguments_only_shows_help(self):
        self.assertIn('db-lab root controller', self.run_all().stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(self.root.glob('*/.env')), [])

    def test_help_and_list_require_no_runtime(self):
        for command in ('--help', 'help', 'list'):
            self.run_all(command)
        self.assertEqual(self.calls(), [])

    def test_default_status_visits_all_projects(self):
        self.run_all('status')
        self.assertEqual(self.names(), list(PROJECT_DIRS))
        self.assertTrue(all(c['args'] == ['status'] for c in self.calls()))

    def test_child_working_directory_and_environment_isolation(self):
        self.run_all('status')
        for call in self.calls():
            self.assertEqual(call['cwd'], str(self.root / PROJECT_DIRS[call['name']]))
            self.assertIsNone(call['leak'])

    def test_start_initializes_before_up(self):
        self.run_all('up', 'mariadb', 'redis')
        self.assertEqual([(c['name'], c['args']) for c in self.calls()],
                         [('mariadb', ['init']), ('mariadb', ['up']),
                          ('redis', ['init']), ('redis', ['up'])])

    def test_up_creates_es_kafka_env_privately(self):
        self.run_all('up', 'es', 'kafka')
        for directory in ('elasticsearch', 'kafka-lab'):
            path = self.root / directory / '.env'
            self.assertEqual(path.read_text(), 'SETTING=original\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.names(), ['elasticsearch', 'kafka'])

    def test_init_preserves_existing_env(self):
        path = self.root / 'elasticsearch/.env'
        path.write_text('CUSTOM=do-not-change\n')
        self.run_all('init', 'es')
        self.assertEqual(path.read_text(), 'CUSTOM=do-not-change\n')

    def test_dangling_env_symlink_is_not_overwritten(self):
        target = self.base / 'missing-env'
        (self.root / 'elasticsearch/.env').symlink_to(target)
        self.run_all('init', 'es', code=1)
        self.assertFalse(target.exists())
        self.assertEqual(self.calls(), [])

    def test_down_reverses_selection_without_purge(self):
        self.run_all('down')
        self.assertEqual(self.names(), list(reversed(PROJECT_DIRS)))
        self.assertTrue(all(c['args'] == ['down'] for c in self.calls()))

    def test_selected_order_and_alias_deduplication(self):
        self.run_all('status', 'redis-lab', 'kafka-lab', 'redis')
        self.assertEqual(self.names(), ['redis', 'kafka'])

    def test_common_aliases(self):
        for alias, expected in [('start', 'up'), ('stop', 'down'), ('ps', 'status'), ('check', 'doctor')]:
            self.run_all(alias, 'es')
            self.assertEqual(self.calls()[-1]['args'], [expected])

    def test_failure_continues_and_returns_nonzero(self):
        p = self.run_all('status', env={'ALL_TEST_FAIL': 'kafka'}, code=1)
        self.assertEqual(self.names(), list(PROJECT_DIRS))
        self.assertIn('kafka: FAILED (exit 7)', p.stderr)
        self.assertIn('redis: OK', p.stderr)

    def test_fail_fast_marks_unattempted_projects(self):
        p = self.run_all('--fail-fast', 'up', env={'ALL_TEST_FAIL': 'kafka'}, code=1)
        self.assertEqual(self.names(), ['elasticsearch', 'kafka'])
        self.assertIn('mariadb: SKIPPED', p.stderr)
        self.assertIn('redis: SKIPPED', p.stderr)

    def test_interrupt_exit_stops_batch(self):
        self.run_all('status', env={'ALL_TEST_FAIL': 'kafka', 'ALL_TEST_FAIL_RC': '130'}, code=130)
        self.assertEqual(self.names(), ['elasticsearch', 'kafka'])

    def test_termination_exit_stops_batch(self):
        self.run_all('status', env={'ALL_TEST_FAIL': 'kafka', 'ALL_TEST_FAIL_RC': '143'}, code=143)
        self.assertEqual(self.names(), ['elasticsearch', 'kafka'])

    def test_failed_initialization_prevents_start(self):
        self.run_all('up', 'mariadb', env={'ALL_TEST_FAIL': 'mariadb', 'ALL_TEST_FAIL_CMD': 'init'}, code=1)
        self.assertEqual(self.calls()[0]['args'], ['init'])
        self.assertEqual(len(self.calls()), 1)

    def test_restart_is_down_init_up(self):
        self.run_all('restart', 'redis')
        self.assertEqual([c['args'] for c in self.calls()], [['down'], ['init'], ['up']])

    def test_failed_shutdown_prevents_restart_start(self):
        self.run_all('restart', 'redis', env={'ALL_TEST_FAIL': 'redis', 'ALL_TEST_FAIL_CMD': 'down'}, code=1)
        self.assertEqual([c['args'] for c in self.calls()], [['down']])

    def test_reset_requires_scope_and_confirmation(self):
        for args in [('reset',), ('reset', 'all'), ('reset', 'redis'), ('reset', '--yes')]:
            self.run_all(*args, code=2)
        self.assertEqual(self.calls(), [])

    def test_reset_uses_each_native_deletion_guard(self):
        self.run_all('reset', 'all', '--yes')
        self.assertEqual([(c['name'], c['args']) for c in self.calls()], [
            ('redis', ['reset', '--yes']),
            ('mariadb', ['reset', '--confirm-delete-lab-data']),
            ('kafka', ['reset', '--yes']),
            ('elasticsearch', ['down', '--purge', '--yes'])])

    def test_reset_limits_scope(self):
        self.run_all('reset', '--yes', 'redis')
        self.assertEqual(self.names(), ['redis'])

    def test_common_down_rejects_deletion_flags(self):
        for flag in ('--purge', '--volumes', '-v', '--yes'):
            self.run_all('down', flag, code=2)
        self.assertEqual(self.calls(), [])

    def test_unknown_target_prevents_all_side_effects(self):
        self.run_all('up', 'es', 'typo', code=2)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(self.root.glob('*/.env')), [])

    def test_unknown_commands_and_options(self):
        for args in [('typo',), ('--parallel', 'up'), ('up', '--nodes', '3')]:
            self.run_all(*args, code=2)
        self.assertEqual(self.calls(), [])

    def test_all_cannot_be_mixed_with_other_targets(self):
        self.run_all('up', 'all', 'redis', code=2)
        self.assertEqual(self.calls(), [])

    def test_missing_entrypoint_preflights_entire_selection(self):
        (self.root / 'redis-lab/lab.sh').unlink()
        self.run_all('up', code=1)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(self.root.glob('*/.env')), [])

    def test_missing_template_preflights_entire_selection(self):
        (self.root / 'kafka-lab/.env.example').unlink()
        self.run_all('up', code=1)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(self.root.glob('*/.env')), [])

    def test_dry_run_creates_nothing_and_executes_nothing(self):
        for args in [('up',), ('reset', 'all', '--yes'), ('test',), ('k8s', 'up')]:
            self.run_all('--dry-run', *args)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(self.root.glob('*/.env')), [])

    def test_trailing_dry_run_supported_for_common_commands(self):
        self.run_all('up', 'redis', '--dry-run')
        self.assertEqual(self.calls(), [])

    def test_dry_run_still_requires_reset_confirmation(self):
        self.run_all('--dry-run', 'reset', 'all', code=2)
        self.assertEqual(self.calls(), [])

    def test_project_first_preserves_native_semantics_and_arguments(self):
        sql = "SELECT 'a b', '$HOME', '; no shell execution';"
        self.run_all('mariadb', 'sql', 'galera1', sql)
        self.assertEqual(self.calls()[-1]['args'], ['sql', 'galera1', sql])
        self.run_all('redis', 'start', 'redis-1')
        self.assertEqual(self.calls()[-1]['args'], ['start', 'redis-1'])
        self.run_all('es', 'reset')
        self.assertEqual(self.calls()[-1]['args'], ['reset'])

    def test_native_exit_status_preserved(self):
        self.run_all('kafka', 'health', env={'ALL_TEST_FAIL': 'kafka'}, code=7)

    def test_project_without_command_gets_native_help(self):
        self.run_all('mariadb')
        self.assertEqual(self.calls()[0]['args'], ['--help'])

    def test_logs_routes_one_project(self):
        self.run_all('logs', 'redis', 'redis-1', '--follow')
        self.assertEqual(self.calls()[0]['args'], ['logs', 'redis-1', '--follow'])

    def test_logs_requires_valid_single_project(self):
        self.run_all('logs', code=2)
        self.run_all('logs', 'all', code=2)
        self.assertEqual(self.calls(), [])

    def test_offline_tests_use_existing_test_scripts(self):
        self.run_all('test')
        self.assertEqual(self.names(), ['test:' + name for name in PROJECT_DIRS])

    def test_es9_is_opt_in_and_not_in_default_batch(self):
        self.run_all('status')
        self.assertNotIn('es9', self.names())
        self.assertEqual(self.names(), list(PROJECT_DIRS))
        self.run_all('status', 'es9')
        self.assertEqual(self.names()[-1], 'es9')
        self.assertEqual(self.calls()[-1]['cwd'], str(self.es9))

    def test_es9_up_creates_env_privately(self):
        self.run_all('up', 'es9')
        path = self.es9 / '.env'
        self.assertEqual(path.read_text(), 'SETTING=original\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.names(), ['es9'])

    def test_es9_reset_uses_purge_guard(self):
        self.run_all('reset', 'es9', '--yes')
        self.assertEqual(self.calls()[-1]['args'], ['down', '--purge', '--yes'])

    def test_es9_native_passthrough(self):
        self.run_all('es9', 'snapshot')
        self.assertEqual(self.calls()[-1]['args'], ['snapshot'])

    def test_symlink_and_foreign_working_directory(self):
        link = self.base / 'linked-all.sh'
        link.symlink_to(os.path.relpath(self.root / 'all.sh', self.base))
        self.run_all('status', 'redis', script=link)
        self.assertEqual(self.calls()[0]['cwd'], str(self.root / 'redis-lab'))

    def test_helm_upgrade_uses_chart_defaults_and_caller_cwd(self):
        self.run_all('k8s', 'up', '-f', './values with spaces.yaml', '--set', 'kafka.enabled=false')
        call = self.calls()[0]
        self.assertEqual(call['cwd'], str(self.base))
        self.assertEqual(call['args'], ['upgrade', '--install', 'db-lab', str(self.root / 'helmchart'),
                                      '--namespace', 'db-lab', '--reset-then-reuse-values', '--create-namespace', '--wait',
                                      '--timeout', '10m', '--kube-context', 'kind-db-lab', '-f', './values with spaces.yaml',
                                      '--set', 'kafka.enabled=false'])

    def test_helm_custom_release_namespace_timeout(self):
        self.run_all('helm', 'up', env={'DB_LAB_RELEASE': 'custom', 'DB_LAB_NAMESPACE': 'training',
                                      'DB_LAB_TIMEOUT': '20m'})
        args = self.calls()[0]['args']
        self.assertEqual(args[2], 'custom')
        self.assertEqual(args[args.index('--namespace') + 1], 'training')
        self.assertEqual(args[args.index('--timeout') + 1], '20m')

    def test_helm_uninstall_requires_confirmation(self):
        self.run_all('k8s', 'down', code=2)
        self.assertEqual(self.calls(), [])
        self.run_all('k8s', 'down', '--yes', '--wait')
        self.assertEqual(self.calls()[0]['args'], ['uninstall', 'db-lab', '--namespace', 'db-lab', '--kube-context', 'kind-db-lab', '--wait'])

    def test_helm_non_mutating_commands_and_aliases(self):
        for command, prefix in [('lint', ['lint']), ('template', ['template', 'db-lab']),
                                ('status', ['status', 'db-lab']), ('history', ['history', 'db-lab']),
                                ('values', ['show', 'values'])]:
            self.run_all('helmchart', command)
            self.assertEqual(self.calls()[-1]['args'][:len(prefix)], prefix)

    def test_helm_exit_status_is_preserved(self):
        self.run_all('k8s', 'status', env={'ALL_TEST_FAIL': 'helm'}, code=7)

    def test_missing_helm_has_clear_error(self):
        # Empty PATH except for the commands used to locate the root.
        minimal = self.base / 'minimal-bin'
        minimal.mkdir()
        (minimal / 'dirname').symlink_to(shutil.which('dirname'))
        p = self.run_all('k8s', 'status', env={'PATH': str(minimal)}, code=127)
        self.assertIn('helm is required', p.stderr)

    def test_help_and_dry_run_do_not_require_python_or_container_tools(self):
        minimal = self.base / 'minimal-bin'
        minimal.mkdir()
        (minimal / 'dirname').symlink_to(shutil.which('dirname'))
        (minimal / 'cat').symlink_to(shutil.which('cat'))
        for args in [(), ('list',), ('--dry-run', 'up'), ('--dry-run', 'k8s', 'up')]:
            self.run_all(*args, env={'PATH': str(minimal)})
        self.assertEqual(self.calls(), [])

    def test_k8s_write_requires_explicit_allowed_context(self):
        self.run_all('k8s','up',env={'DB_LAB_CONTEXT':''},code=2)
        self.run_all('k8s','up',env={'DB_LAB_CONTEXT':'production'},code=2)
        self.assertEqual(self.calls(),[])
    def test_k8s_target_overrides_and_protected_namespace_rejected(self):
        for args in [('--namespace','default'),('--kube-context=production',),('-ndefault',)]:
            self.run_all('k8s','up',*args,code=2)
        self.run_all('k8s','up',env={'DB_LAB_NAMESPACE':'kube-system'},code=2)
        self.assertEqual(self.calls(),[])
    def test_k8s_upgrade_cannot_bypass_plan_checks(self):
        for flag in ['--force','--wait=false','--skip-schema-validation','--post-renderer=evil','--reuse-values']:
            self.run_all('k8s','up',flag,code=2)
        self.assertEqual(self.calls(),[])
    def test_preflight_failure_starts_nothing(self):
        (self.root/'scripts/control.py').write_text('import sys\nraise SystemExit(1)\n')
        self.run_all('up','es','kafka',code=1)
        self.assertEqual(self.calls(),[])
        self.assertFalse((self.root/'elasticsearch/.env').exists())
    def test_health_json_is_routed_without_status_headings(self):
        (self.root/'scripts/control.py').write_text('print(\'{"ready": false}\')\nraise SystemExit(1)\n')
        p=self.run_all('health','es','--json',code=1)
        self.assertFalse(json.loads(p.stdout)['ready'])
    def test_lock_rejects_competing_lifecycle(self):
        import fcntl
        (self.root/'.state').mkdir()
        with (self.root/'.state/all.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_all('down','redis',code=1)
        self.assertEqual(self.calls(),[])
    def test_real_receipt_excludes_native_sql_arguments(self):
        shutil.copy2(SOURCE.parent/'scripts/control.py',self.root/'scripts/control.py')
        self.run_all('mariadb','sql','galera1','SELECT super_secret_value;')
        receipts=list((self.root/'reports').glob('all-*.json'))
        self.assertEqual(len(receipts),1)
        data=json.loads(receipts[0].read_text())
        self.assertEqual(data['status'],'completed')
        self.assertFalse(data['health_verified'])
        self.assertNotIn('super_secret_value',receipts[0].read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
