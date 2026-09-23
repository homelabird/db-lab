#!/usr/bin/env python3
"""Set Kibana server and UI credentials after an Elasticsearch cluster is ready."""
import os
from urllib.parse import quote

from lablib_core import ESClient


def main():
    client = ESClient()
    kibana_password = os.environ["KIBANA_PASSWORD"]
    username = os.environ["KIBANA_LOGIN_USERNAME"]
    password = os.environ["KIBANA_LOGIN_PASSWORD"]
    client.request("PUT", "/_security/user/kibana_system/_password",
                   {"password": kibana_password})

    if os.environ.get("KIBANA_STACK_MANAGEMENT_ENABLED", "true").lower() == "false":
        role = {
            "cluster": ["monitor"],
            "indices": [{"names": ["*"], "privileges": ["read", "view_index_metadata"]}],
            "applications": [{
                "application": "kibana-.kibana",
                "privileges": [
                    "feature_dashboard.read", "feature_discover.read",
                    "feature_visualize.read", "feature_dev_tools.read",
                ] + (["feature_monitoring.read"]
                     if os.environ.get("KIBANA_STACK_MONITORING_ENABLED", "true").lower() == "true"
                     else []),
                "resources": ["*"],
            }],
        }
        client.request("PUT", "/_security/role/lab_kibana_without_management", role)
        roles = ["lab_kibana_without_management"]
    else:
        roles = ["kibana_admin"]

    client.request("PUT", "/_security/user/" + quote(username, safe=""), {
        "password": password,
        "roles": roles,
        "enabled": True,
    })
    print("[ok] Kibana server password and UI role configured.")


if __name__ == "__main__":
    main()
