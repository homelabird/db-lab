"""YAML/source checks only: optional PyYAML, never a replacement for real Compose."""
from pathlib import Path
import unittest
try:
    import yaml
except ImportError:
    yaml = None
ROOT = Path(__file__).resolve().parents[1]

@unittest.skipIf(yaml is None, "PyYAML not installed; real Compose config is checked by doctor/up")
class ComposeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = yaml.safe_load((ROOT / "compose.yaml").read_text())

    def test_six_default_services(self):
        self.assertEqual(sum(not spec.get("profiles") for spec in self.data["services"].values()), 6)

    def test_only_api_is_published_to_host_loopback(self):
        for name, spec in self.data["services"].items():
            if name == "api":
                self.assertEqual(spec["ports"], ["127.0.0.1:${API_PORT:-18090}:8080"])
            else:
                self.assertNotIn("ports", spec)

    def test_named_volume_mounts_have_declarations(self):
        declared = set(self.data["volumes"])
        for spec in self.data["services"].values():
            for mount in spec.get("volumes", []):
                self.assertIn(mount.split(":")[0], declared)

    def test_kafka_one_node_has_valid_internal_listener(self):
        env = self.data["services"]["kafka"]["environment"]
        self.assertEqual(env["KAFKA_ADVERTISED_LISTENERS"], "PLAINTEXT://kafka:9092")
        self.assertEqual(env["KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR"], "1")
        self.assertEqual(env["KAFKA_PROCESS_ROLES"], "broker,controller")

    def test_wrong_password_only_changes_api(self):
        api = self.data["services"]["api"]["environment"]
        worker = self.data["services"]["worker"]["environment"]
        self.assertNotEqual(api["SQL_PASSWORD"], worker["SQL_PASSWORD"])
        self.assertEqual(worker["SQL_PASSWORD"], "${SQL_PASSWORD}")

    def test_each_service_has_memory_bound(self):
        for name, spec in self.data["services"].items():
            self.assertIn("mem_limit", spec, name)

    def test_no_root_host_mount_or_privilege(self):
        for name, spec in self.data["services"].items():
            self.assertNotIn("privileged", spec)
            self.assertNotIn("network_mode", spec)
            self.assertNotIn("container_name", spec)
            for mount in spec.get("volumes", []):
                self.assertFalse(mount.startswith("/"))
                self.assertNotIn("docker.sock", mount)

    def test_spare_is_distinct_volume(self):
        services = self.data["services"]
        self.assertNotEqual(services["redis"]["volumes"], services["redis-spare"]["volumes"])
        self.assertEqual(services["redis-spare"]["profiles"], ["spare"])
