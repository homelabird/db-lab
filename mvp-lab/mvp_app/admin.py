"""Idempotent first setup and non-destructive search projection rebuild."""
import argparse
from .adapters import dependencies
from .core import emit
from .observability import safe_error


def rebuild(repo, search):
    search.initialize()
    result = {"attempted": 0, "indexed": 0, "duplicates": 0, "skipped_older_version": 0, "errors": 0}
    for order in repo.scan():
        result["attempted"] += 1
        try:
            outcome = search.index(order)
        except Exception as exc:
            result["errors"] += 1
            emit("search_rebuild_incomplete", **result, **safe_error(exc))
            raise
        if outcome == "older_version_ignored":
            result["skipped_older_version"] += 1
        elif outcome == "duplicate_ignored":
            result["duplicates"] += 1
        elif outcome == "indexed":
            result["indexed"] += 1
        else:
            raise RuntimeError("Unknown projection outcome")
    search.refresh()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "rebuild-search"])
    args = parser.parse_args()
    repo, cache, search, broker = dependencies()
    try:
        if args.action == "init":
            for name, fn in [("mariadb", repo.initialize), ("kafka", broker.initialize),
                             ("elasticsearch", search.initialize), ("redis", cache.status)]:
                fn()
                emit("initialized", dependency=name)
        else:
            emit("search_rebuilt", **rebuild(repo, search), index=search.s.es_index)
    except Exception as exc:
        emit("admin_failed", action=args.action, **safe_error(exc))
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
