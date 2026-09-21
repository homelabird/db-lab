#!/usr/bin/env python3
"""Real HTTP end-to-end acceptance probe. Creates one fake order; never deletes data."""
import argparse
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A loopback entrypoint must not forward credentials/writes elsewhere.


def read_object(response):
    raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise RuntimeError("Probe response exceeds the 1MiB limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Probe expected a JSON object")
    return value


def check_order(value):
    fields = {"id", "item", "quantity", "unit_price", "total", "status", "version", "created_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError("Probe expected the complete public order fields")
    if any(type(value[k]) is not int for k in ("quantity", "unit_price", "total", "version")):
        raise RuntimeError("Probe order has non-integer numeric fields")
    if value["total"] != value["quantity"] * value["unit_price"]:
        raise RuntimeError("Probe order total is inconsistent")
    try:
        if str(uuid.UUID(value["id"])) != value["id"]:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("Probe expected a canonical order UUID") from exc
    return value


def validate_project(project):
    if not isinstance(project, str) or not re.fullmatch(r"db-lab-mvp(?:-[a-z0-9-]{1,24})?", project):
        raise ValueError("Expected a specific db-lab-mvp project name")
    return project


def verify_identity(value, project):
    validate_project(project)
    if (not isinstance(value, dict) or value.get("application") != "db-lab-mvp"
            or type(value.get("study_api")) is not int or value["study_api"] != 2
            or value.get("project") != project):
        raise RuntimeError("MVP HTTP project identity mismatch; no order was submitted")


def probe(base, timeout=60, *, expected_project="db-lab-mvp"):
    validate_project(expected_project)
    url = urlsplit(base)
    if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("Probe requires a plain HTTP loopback URL without credentials/path/query/fragment")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ValueError("Probe timeout must be 1..300 seconds")
    base = base.rstrip("/")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    started = time.monotonic()
    deadline = started + timeout
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Probe time budget exhausted; no further HTTP request was started")
        return min(8, left)
    # A loopback address alone is not proof that this is the intended lab.
    with opener.open(base + "/api/study/info", timeout=remaining()) as response:
        verify_identity(read_object(response), expected_project)
    key = str(uuid.uuid4())
    submitted = {"item": "smoke-" + key[:8], "quantity": 1, "unit_price": 100}
    request = Request(base + "/api/orders", data=json.dumps(submitted).encode(), headers={
        "Content-Type": "application/json", "Idempotency-Key": key})
    with opener.open(request, timeout=remaining()) as response:
        created = read_object(response)
        order = check_order(created.get("order"))
        if (created.get("created") is not True or any(order[k] != v for k, v in submitted.items())
                or order["status"] != "created" or order["version"] != 1):
            raise RuntimeError("Create acknowledgement differs from submitted request")
    # Verify same-key retry does not create a second order.
    with opener.open(request, timeout=remaining()) as response:
        duplicate = read_object(response)
    if duplicate.get("created") is not False or check_order(duplicate.get("order")) != order:
        raise RuntimeError("Idempotency acceptance failed")
    last = None
    while time.monotonic() - started < timeout:
        try:
            with opener.open(base + "/api/search?q=" + order["id"], timeout=remaining()) as response:
                result = read_object(response)
            matches = [o for o in result["orders"] if o.get("id") == order["id"] and o.get("version") == order["version"]]
            if matches:
                if len(matches) != 1 or matches[0] != order:
                    raise RuntimeError("Search content differs from SQL acknowledgement (not only ID/version)")
                with opener.open(base + "/api/orders/" + order["id"] + "?fresh=1", timeout=remaining()) as response:
                    detail = read_object(response)
                if detail["order"] != order:
                    raise RuntimeError("Authoritative data differs from submitted order")
                return {"passed": True, "project": expected_project, "project_identity_checked": True, "order_id": order["id"], "idempotency_checked": True, "submitted_payload_checked": True, "full_retry_payload_checked": True,
                        "order_to_search_seconds": round(time.monotonic() - started, 3),
                        "scope": "one real order through SQL/outbox/Kafka/ES; not HA or performance certification"}
        except HTTPError as exc:
            if exc.code not in {429, 502, 503, 504}:
                raise RuntimeError("Search/detail returned a non-retryable HTTP status: " + str(exc.code)) from None
            last = "HTTP " + str(exc.code)
        except (URLError, TimeoutError) as exc:
            last = type(exc).__name__
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"Search did not catch up. order_id={order['id']} last_error={last}; inspect outbox and lag")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18090")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--project", default="db-lab-mvp", help="Exact expected project; checked before the first write")
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.url.rstrip("/"), args.timeout, expected_project=args.project), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"passed": False, "error": type(exc).__name__, "detail": str(exc)}))
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
