"""Small local HTTP boundary: strict JSON and bounded connection threads.

This does not add authentication/TLS or turn http.server into a production server.
The cap bounds accepted request threads, not DB connections or diagnostic workers.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import math
import threading
from urllib.parse import urlsplit

from .core import Problem

MAX_CONNECTIONS = 32
MAX_BODY = 16384


def local_host(headers):
    hosts = headers.get_all("Host", [])
    if len(hosts) != 1:
        raise Problem(400, "single_host_header_required")
    host = hosts[0]
    try:
        parsed = urlsplit("//" + host)
        port = parsed.port  # also validates malformed/non-numeric ports
        valid = (parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                 and parsed.username is None and parsed.password is None
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and not any(c.isspace() for c in host)
                 and (port is None or 1 <= port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise Problem(403, "local_host_only_use_ssh_tunnel")
    if len(headers.get_all("Idempotency-Key", [])) > 1:
        raise Problem(400, "single_idempotency_key_required")
    return host


def json_body_length(headers):
    if headers.get_all("Transfer-Encoding"):
        raise Problem(400, "chunked_body_not_supported")
    if headers.get_content_type() != "application/json":
        raise Problem(415, "application_json_required")
    lengths = headers.get_all("Content-Length", [])
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
        raise Problem(400, "invalid_content_length")
    length = int(lengths[0])
    if not 0 < length <= MAX_BODY:
        raise Problem(413, "body_must_be_1_to_16384_bytes")
    return length


def decode_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("non-finite JSON number")

    def validate(value, depth=0):
        if depth > 16:
            raise ValueError("JSON nesting limit")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        if isinstance(value, str):
            value.encode("utf-8", errors="strict")
        elif isinstance(value, dict):
            for key, item in value.items():
                validate(key, depth + 1)
                validate(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                validate(item, depth + 1)

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=nonfinite)
        validate(result)
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Problem(400, "invalid_json") from exc


def body_fields(data, allowed):
    if not isinstance(data, dict):
        raise Problem(400, "json_object_required")
    if set(data) - set(allowed):
        raise Problem(400, "unexpected_request_fields")


class BoundedHTTPServer(ThreadingHTTPServer):
    """Reject overload before spawning a thread; no unbounded executor queue."""
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, *, max_connections=MAX_CONNECTIONS):
        if type(max_connections) is not int or not 1 <= max_connections <= 64:
            raise ValueError("max_connections must be 1..64")
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            body = b'{"error":"api_capacity_exceeded"}'
            response = (b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Content-Type: application/json\r\nConnection: close\r\n"
                        b"Retry-After: 1\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            try:
                request.settimeout(0.25)
                request.sendall(response)
            except OSError:
                pass  # a peer disconnect is not another reason to consume a slot
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
