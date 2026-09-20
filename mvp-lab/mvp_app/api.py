"""Local study HTTP API and a single static page. Not a production HTTP server."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
import json
import os
import re
import time
from pathlib import Path
import socket
from urllib.parse import parse_qs, urlsplit
import uuid
from .adapters import dependencies
from .core import Problem, Storefront, emit, request_key, REQUEST_CONTEXT
from .observability import safe_error
from .inspection import inspect_order
from .http_policy import BoundedHTTPServer, body_fields, decode_json, json_body_length, local_host


def backend(name, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Problem:
        raise
    except Exception as exc:
        emit("dependency_error", dependency=name, **safe_error(exc))
        category = safe_error(exc).get("category")
        public_code = name + "_" + category if category in {"lock_wait_timeout", "deadlock", "authentication"} else name + "_unavailable"
        raise Problem(503, public_code) from exc


class Application:
    def __init__(self, repo, cache, search, broker):
        self.repo, self.cache, self.search, self.broker = repo, cache, search, broker
        self.shop = Storefront(repo, cache, search)

    def close(self):
        for client in (self.cache, self.search):
            close = getattr(client, "close", None)
            if close is not None:
                close()

    def diagnostics(self):
        def check(name, fn):
            try:
                return name, {"reachable": True, **fn()}
            except Exception as exc:
                return name, {"reachable": False, **safe_error(exc)}
        checks = [("mariadb", self.repo.status), ("redis", self.cache.status),
                  ("elasticsearch", self.search.status), ("kafka", self.broker.status)]
        # Driver calls have finite timeouts. This is reachability/backlog, NOT a smoke test.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(check, *pair) for pair in checks]
            data = dict(f.result() for f in futures)
        return {"dependencies": data, "dependencies_reachable": all(x["reachable"] for x in data.values()),
                "note": "Reachability is not end-to-end health. Run ./all.sh mvp smoke.",
                "consistency": "MariaDB is authoritative; cache and search can be stale."}

    def route(self, method, path, query, data=None, key=None):
        if method == "GET" and path == "/api/study/info":
            return 200, {"application": "db-lab-mvp", "study_api": 2,
                         "project": os.environ.get("STUDY_PROJECT", "unbound")}
        if method == "GET" and path.startswith("/api/study/order/"):
            return 200, inspect_order(self.repo, self.cache, self.search, path[len("/api/study/order/"):])
        if method == "GET" and path.startswith("/api/study/key/"):
            key = path[len("/api/study/key/"):]
            if not key.startswith("sim-"):
                raise Problem(400, "simulation_key_required")
            return 200, {"order": backend("mariadb", self.repo.lookup_key, request_key(key)),
                         "source": "mariadb", "read_only": True}
        if method == "GET" and path == "/health/live":
            return 200, {"alive": True}
        if method == "GET" and path == "/health/ready":
            backend("mariadb", self.repo.ping)
            return 200, {"database_reachable": True, "scope": "SQL connectivity only"}
        if method == "GET" and path == "/api/diagnostics":
            return 200, self.diagnostics()
        if method == "GET" and path == "/api/orders":
            return 200, {"orders": backend("mariadb", self.repo.list_orders), "source": "mariadb"}
        if method == "POST" and path == "/api/orders":
            body_fields(data, ("item", "quantity", "unit_price"))
            order, created = backend("mariadb", self.shop.create, data, key)
            return (201 if created else 200), {"order": order, "created": created,
                                               "search_consistency": "eventual"}
        if path.startswith("/api/orders/"):
            order_id = path[len("/api/orders/"):]
            if method == "GET":
                return 200, backend("mariadb", self.shop.detail, order_id, query.get("fresh", ["0"])[0] == "1")
            if method == "PATCH":
                body_fields(data, ("status", "expected_version"))
                return 200, {"order": backend("mariadb", self.shop.update, order_id, data)}
        if method == "GET" and path == "/api/search":
            q = query.get("q", [""])[0]
            if len(q) > 100:
                raise Problem(400, "query_too_long")
            return 200, backend("elasticsearch", self.search.find, q)
        raise Problem(404, "route_not_found")


def make_handler(app_factory):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "DBLabMVP/1"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass  # Structured request events below, without query bodies or secrets.

        def reply(self, status, payload, request_id, content_type="application/json; charset=utf-8"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-ID", request_id)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
            self.send_header("Connection", "close")
            if isinstance(payload, dict) and "source" in payload:
                self.send_header("X-Data-Source", payload["source"])
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def handle_api(self):
            request_id = str(uuid.uuid4())
            started = time.monotonic()
            context = {"request_id": request_id}
            run_id = self.headers.get("X-Study-Run", "")
            if re.fullmatch(r"sim-[a-f0-9]{12}", run_id):
                context["study_run"] = run_id
            token = REQUEST_CONTEXT.set(context)
            app = None
            try:
                host = local_host(self.headers)
                if self.command in {"POST", "PATCH"}:
                    origin = self.headers.get("Origin")
                    if origin and origin != "http://" + host:
                        raise Problem(403, "cross_origin_write_rejected")
                parsed = urlsplit(self.path)
                if self.command == "GET" and parsed.path == "/":
                    self.reply(200, Path(__file__).with_name("index.html").read_bytes(), request_id,
                               "text/html; charset=utf-8")
                    return
                data = None
                if self.command in {"POST", "PATCH"}:
                    length = json_body_length(self.headers)
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise Problem(400, "invalid_json")
                    data = decode_json(raw)
                try:
                    query = parse_qs(parsed.query, max_num_fields=20)
                except ValueError as exc:
                    raise Problem(400, "too_many_query_parameters") from exc
                # Per-request clients avoid sharing requests.Session across HTTP threads.
                app = app_factory()
                status, payload = app.route(self.command, parsed.path, query,
                                                     data, self.headers.get("Idempotency-Key"))
                self.reply(status, payload, request_id)
                emit("http_request", request_id=request_id, method=self.command, path=parsed.path, status=status)
            except Problem as exc:
                self.reply(exc.status, {"error": exc.code, "request_id": request_id}, request_id)
                emit("http_error", request_id=request_id, status=exc.status, code=exc.code)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                emit("http_client_disconnected", request_id=request_id)
            except Exception as exc:
                emit("http_unexpected_error", request_id=request_id, error=type(exc).__name__)
                self.reply(500, {"error": "internal_error", "request_id": request_id}, request_id)
            finally:
                try:
                    if app is not None:
                        app.close()
                finally:
                    emit("http_completed", elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                    REQUEST_CONTEXT.reset(token)

        do_GET = handle_api
        do_POST = handle_api
        do_PATCH = handle_api

    return Handler


def main():
    server = BoundedHTTPServer(("0.0.0.0", 8080), make_handler(lambda: Application(*dependencies())))
    server.daemon_threads = True
    emit("api_started", port=8080, scope="loopback-published local study only")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
