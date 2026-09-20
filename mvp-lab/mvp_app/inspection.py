"""Observational comparison; not an atomic cross-database snapshot or repair."""
from .core import ORDER_FIELDS, identifier, validate_event
from .observability import safe_error

def differences(reference, candidate):
    if not isinstance(reference, dict) or not isinstance(candidate, dict):
        return list(ORDER_FIELDS)
    return [k for k in ORDER_FIELDS if k not in candidate or k not in reference
            or type(candidate[k]) is not type(reference[k]) or candidate[k] != reference[k]]

def inspect_order(repo, cache, search, order_id):
    identifier(order_id)
    def read(fn):
        try:
            return {"reachable": True, "order": fn()}
        except Exception as exc:
            return {"reachable": False, **safe_error(exc)}
    first = read(lambda: repo.get(order_id))
    cached = read(lambda: cache.get(order_id))
    def find_exact():
        rows = [o for o in search.find(order_id)["orders"] if o.get("id") == order_id]
        if len(rows) > 1:
            raise ValueError("duplicate search document")
        return rows[0] if rows else None
    indexed = read(find_exact)
    last = read(lambda: repo.get(order_id))
    stable = first.get("reachable") and last.get("reachable") and first.get("order") == last.get("order")
    original = last.get("order")
    result = {"order_id": order_id, "sql_stable_during_observation": bool(stable),
              "mariadb": last, "redis": cached, "elasticsearch": indexed,
              "read_only": True, "note": "No cache fill or refresh. These reads are not an atomic snapshot."}
    valid = False
    if original:
        try:
            validate_event({"schema_version": 1, "event_id": order_id, "order": original})
            valid = True
        except Exception:
            pass
    result["authoritative_valid"] = valid
    for name in ("redis", "elasticsearch"):
        source = result[name]
        if not stable or not valid or not source["reachable"]:
            source["comparison"] = "unknown"
        elif source.get("order") is None:
            source["comparison"] = "missing"
        else:
            source["different_fields"] = differences(original, source["order"])
            source["comparison"] = "mismatch" if source["different_fields"] else "match"
    return result
