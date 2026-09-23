#!/usr/bin/env python3
"""Read-only end-to-end verification for a running Elasticsearch lab."""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from lablib_core import ESClient, write_json


def verify(args):
    client = ESClient(timeout=40)
    info = client.assert_lab()
    if not info["version"]["number"].startswith(args.es_version):
        raise RuntimeError(f"Expected Elasticsearch {args.es_version}.*, got {info['version']['number']}.")
    print(f"[PASS] Elasticsearch {info['version']['number']} cluster identity")

    expected = set(args.nodes.split(","))
    nodes = client.request("GET", "/_nodes")
    actual = {node["name"] for node in nodes["nodes"].values()}
    if actual != expected:
        raise RuntimeError(f"Expected nodes {sorted(expected)}, got {sorted(actual)}.")
    health = client.request(
        "GET", "/_cluster/health?wait_for_status=green&wait_for_no_relocating_shards=true&"
        "wait_for_no_initializing_shards=true&timeout=30s", timeout=35)
    if (health.get("timed_out") or health.get("status") != "green"
            or health.get("number_of_nodes") != len(expected)
            or health.get("unassigned_shards") != 0):
        raise RuntimeError(f"Cluster is not fully green: {json.dumps(health, sort_keys=True)}")
    print(f"[PASS] {len(actual)} expected nodes; cluster green; no unassigned shards")

    repo_name = quote(os.getenv("SNAPSHOT_REPO_NAME", "lab-snapshots"), safe="")
    repo = client.request("GET", f"/_snapshot/{repo_name}")
    settings = repo.get(os.getenv("SNAPSHOT_REPO_NAME", "lab-snapshots"), {}).get("settings", {})
    location = os.getenv("SNAPSHOT_PATH", "/usr/share/elasticsearch/snapshots")
    if settings.get("location") != location:
        raise RuntimeError(f"Snapshot repository location is {settings.get('location')!r}; expected {location!r}.")
    result = client.request("POST", f"/_snapshot/{repo_name}/_verify")
    node_names = {nodes["nodes"][identity]["name"] for identity in result.get("nodes", {})
                  if identity in nodes["nodes"]}
    if node_names != expected:
        raise RuntimeError(f"Snapshot repository verified on {sorted(node_names)}; expected {sorted(expected)}.")
    print(f"[PASS] Snapshot repository verified on {len(node_names)} nodes")

    base = f"http://127.0.0.1:{args.kibana_port}/api/status"
    headers = {"kbn-xsrf": "verify-install"}
    if os.getenv("XPACK_SECURITY_ENABLED", "false").lower() == "true":
        username = os.getenv("KIBANA_LOGIN_USERNAME", "")
        password = os.getenv("KIBANA_LOGIN_PASSWORD", "")
        if not username or not password:
            raise RuntimeError("Kibana verification needs KIBANA_LOGIN_USERNAME and KIBANA_LOGIN_PASSWORD.")
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        headers["Authorization"] = "Basic " + token
    request = urllib.request.Request(base, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Kibana status endpoint is unavailable: {type(exc).__name__}.") from exc
    overall = status.get("status", {}).get("overall", {})
    level = overall.get("level") or overall.get("state")
    if level not in ("available", "green"):
        raise RuntimeError(f"Kibana is responding but overall status is {level!r}, expected available/green.")
    print(f"[PASS] Kibana status {level} on port {args.kibana_port}")

    if args.cerebro_port:
        request = urllib.request.Request(f"http://127.0.0.1:{args.cerebro_port}/")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError(f"Cerebro returned HTTP {response.status}.")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Cerebro is unavailable: {type(exc).__name__}.") from exc
        print(f"[PASS] Cerebro HTTP service on port {args.cerebro_port}")

    return {"status": "PASS", "checked_at": datetime.now(timezone.utc).isoformat(),
            "elasticsearch_version": info["version"]["number"], "cluster_uuid": info["cluster_uuid"],
            "nodes": sorted(actual), "cluster_status": "green", "unassigned_shards": 0,
            "snapshot_repository": os.getenv("SNAPSHOT_REPO_NAME", "lab-snapshots"),
            "snapshot_verified_nodes": sorted(node_names), "kibana_status": level,
            "cerebro_checked": bool(args.cerebro_port)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--es-version", required=True)
    parser.add_argument("--nodes", default="es01,es02,es03,es04,es05")
    parser.add_argument("--kibana-port", required=True, type=int)
    parser.add_argument("--cerebro-port", type=int)
    args = parser.parse_args()
    report = Path(os.environ["LAB_ROOT"]) / "reports/install-verification.json"
    try:
        result = verify(args)
    except Exception as exc:
        write_json(report, {"status": "FAIL", "checked_at": datetime.now(timezone.utc).isoformat(),
                            "error_type": type(exc).__name__, "error": str(exc)[:2000]})
        sys.exit(f"[FAIL] {exc}\nReport: {report}")
    write_json(report, result)
    print(f"[PASS] Installation verification report: {report}")


if __name__ == "__main__":
    main()
