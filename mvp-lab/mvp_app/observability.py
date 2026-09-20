"""Allowlisted diagnostics: never serialize exception messages, DSNs or response bodies."""
import re

ES_TYPES = {"mapper_parsing_exception", "strict_dynamic_mapping_exception",
            "unavailable_shards_exception", "index_not_found_exception",
            "version_conflict_engine_exception", "cluster_block_exception",
            "es_rejected_execution_exception", "illegal_argument_exception",
            "resource_already_exists_exception", "security_exception"}
SQL_CODES = {1045: "authentication", 1205: "lock_wait_timeout", 1213: "deadlock",
             2003: "connection", 2006: "connection_lost", 2013: "connection_lost"}

def safe_error(exc):
    fields = {"error": type(exc).__name__}
    code = getattr(exc, "code", None)
    if not isinstance(code, (int, str)):
        code = exc.args[0] if exc.args and type(exc.args[0]) is int else None
    if isinstance(code, str) and not re.fullmatch(r"[a-z0-9_]{1,80}", code):
        code = None
    fields["code"] = code
    if type(code) is int and code in SQL_CODES:
        fields["category"] = SQL_CODES[code]
    response = getattr(exc, "response", None)
    # requests.Response is false for HTTP >=400; explicitly test None.
    if response is not None:
        status = getattr(response, "status_code", None)
        if type(status) is int:
            fields["http_status"] = status
        try:
            error = response.json().get("error", {})
            kind = error.get("type") if isinstance(error, dict) else None
            fields["error_type"] = kind if kind in ES_TYPES else "unclassified_http_error"
        except (ValueError, TypeError, AttributeError):
            fields["error_type"] = "unclassified_http_error"
    return fields
