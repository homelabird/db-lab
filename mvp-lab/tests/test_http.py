"""Real loopback HTTP tests with fake DB dependencies; not real DB smoke tests."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from urllib.parse import urlsplit
from mvp_app.api import Application, make_handler
from mvp_app.core import relay_once, project_one
from tools.probe import probe
from fakes import MemoryRepo, MemoryCache, MemorySearch, MemoryBroker

DATA = {"item": "web study", "quantity": 1, "unit_price": 12}

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(lambda: cls.app))
        cls.thread = threading.Thread(target=lambda: cls.server.serve_forever(poll_interval=0.01), daemon=True)
        cls.thread.start(); cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def setUp(self):
        self.repo, self.cache, self.search, self.broker = MemoryRepo(), MemoryCache(), MemorySearch(), MemoryBroker()
        type(self).app = Application(self.repo, self.cache, self.search, self.broker)

    def request(self, method, path, data=None, headers=None, raw=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        h = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, body, h)
        response = conn.getresponse(); text = response.read()
        result = json.loads(text) if "application/json" in response.getheader("Content-Type", "") else text.decode()
        return response.status, result, response

    def create(self):
        code, result, _ = self.request("POST", "/api/orders", DATA)
        self.assertEqual(code, 201)
        return result["order"]

    def test_home_has_ui_no_db_required(self):
        self.repo.down = True
        code, text, response = self.request("GET", "/")
        self.assertEqual(code, 200); self.assertIn("작은 주문 시스템", text)
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))

    def test_liveness_independent_of_db(self):
        self.repo.down = True
        self.assertEqual(self.request("GET", "/health/live")[0], 200)
        self.assertEqual(self.request("GET", "/health/ready")[0], 503)

    def test_create_and_source_header(self):
        order = self.create()
        first = self.request("GET", "/api/orders/" + order["id"])
        second = self.request("GET", "/api/orders/" + order["id"])
        self.assertEqual(first[2].getheader("X-Data-Source"), "mariadb")
        self.assertEqual(second[2].getheader("X-Data-Source"), "redis")

    def test_idempotent_http_retry(self):
        header = {"Idempotency-Key": "retry-1"}
        first = self.request("POST", "/api/orders", DATA, header)
        second = self.request("POST", "/api/orders", DATA, header)
        self.assertEqual((first[0], second[0]), (201, 200))
        self.assertEqual(first[1]["order"]["id"], second[1]["order"]["id"])

    def test_failed_cache_falls_back_http(self):
        order = self.create(); self.cache.down = True
        code, data, _ = self.request("GET", "/api/orders/" + order["id"])
        self.assertEqual(code, 200); self.assertEqual(data["source"], "mariadb")

    def test_sql_down_blocks_writes_but_cached_read_survives(self):
        order = self.create(); self.request("GET", "/api/orders/" + order["id"])
        self.repo.down = True
        self.assertEqual(self.request("POST", "/api/orders", DATA)[0], 503)
        self.assertEqual(self.request("GET", "/api/orders/" + order["id"])[0], 200)
        self.assertEqual(self.request("GET", "/api/orders/" + order["id"] + "?fresh=1")[0], 503)

    def test_search_failure_is_explicit_503(self):
        self.search.down = True
        code, data, _ = self.request("GET", "/api/search")
        self.assertEqual(code, 503); self.assertEqual(data["error"], "elasticsearch_unavailable")

    def test_patch_and_version_conflict(self):
        order = self.create(); path = "/api/orders/" + order["id"]
        self.assertEqual(self.request("PATCH", path, {"status": "paid", "expected_version": 1})[0], 200)
        self.assertEqual(self.request("PATCH", path, {"status": "shipped", "expected_version": 1})[0], 409)

    def test_diagnostics_reports_failure_without_whole_page_failure(self):
        self.cache.down = True
        code, data, _ = self.request("GET", "/api/diagnostics")
        self.assertEqual(code, 200)
        self.assertFalse(data["dependencies_reachable"])
        self.assertTrue(data["dependencies"]["mariadb"]["reachable"])
        self.assertFalse(data["dependencies"]["redis"]["reachable"])

    def test_json_validation(self):
        self.assertEqual(self.request("POST", "/api/orders", raw=b"{")[0], 400)
        self.assertEqual(self.request("POST", "/api/orders", raw=b"null")[0], 400)
        self.assertEqual(self.request("POST", "/api/orders", raw=b"\xff")[0], 400)

    def test_bad_content_type(self):
        self.assertEqual(self.request("POST", "/api/orders", DATA, {"Content-Type": "text/plain"})[0], 415)

    def test_large_body(self):
        self.assertEqual(self.request("POST", "/api/orders", raw=b"x" * 17000)[0], 413)

    def test_foreign_origin_rejected(self):
        self.assertEqual(self.request("POST", "/api/orders", DATA, {"Origin": "https://evil.example"})[0], 403)

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.request("GET", "/api/orders", headers={"Host": "evil.example"})[0], 403)

    def test_bad_id_and_route(self):
        self.assertEqual(self.request("GET", "/api/orders/../../anything")[0], 400)
        self.assertEqual(self.request("GET", "/unknown")[0], 404)

    def test_probe_against_http_with_fake_pipeline(self):
        stop = threading.Event()
        def pump():
            while not stop.wait(0.01):
                if relay_once(self.repo, self.broker):
                    project_one(self.broker.events[-1], self.search, self.cache, lambda: None)
        thread = threading.Thread(target=pump, daemon=True); thread.start()
        try:
            result = probe(f"http://127.0.0.1:{self.port}", 5)
            self.assertTrue(result["passed"])
            self.assertEqual(len(self.repo.orders), 1)
        finally:
            stop.set(); thread.join(timeout=2)
