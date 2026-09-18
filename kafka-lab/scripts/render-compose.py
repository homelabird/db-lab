#!/usr/bin/env python3
"""Render a node-count-specific Compose file without putting node count in Compose."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def external_port(node: int) -> int:
    if node <= 3:
        return 19092 + (node - 1) * 10000
    # Preserve the original three ports and keep nodes 4..100 below 65535.
    return 40000 + (node - 4) * 100


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("zk", "kraft"), required=True)
    p.add_argument("--nodes", type=int, required=True)
    p.add_argument("--lab-name", required=True)
    p.add_argument("--cp-version", required=True)
    p.add_argument("--advertised-host", required=True)
    p.add_argument("--bind-ip", required=True)
    p.add_argument("--cluster-id")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    n = a.nodes
    rf = min(n, 3)
    min_isr = 2 if rf >= 2 else 1
    brokers = [f"kafka{i}:9092" for i in range(1, n + 1)]
    services: dict = {}
    volumes: dict = {}

    def labels():
        return {"io.kzk.lab": a.lab_name}

    if a.mode == "zk":
        zk_servers = ";".join(f"zk{i}:2888:3888" for i in range(1, n + 1))
        zk_connect = ",".join(f"zk{i}:2181" for i in range(1, n + 1))
        for i in range(1, n + 1):
            services[f"zk{i}"] = {
                "image": f"docker.io/confluentinc/cp-zookeeper:{a.cp_version}",
                "container_name": f"{a.lab_name}-zk{i}", "hostname": f"zk{i}",
                "restart": "no", "labels": labels(),
                "environment": {
                    "ZOOKEEPER_SERVER_ID": str(i), "ZOOKEEPER_CLIENT_PORT": "2181",
                    "ZOOKEEPER_TICK_TIME": "2000", "ZOOKEEPER_INIT_LIMIT": "10",
                    "ZOOKEEPER_SYNC_LIMIT": "5", "ZOOKEEPER_SERVERS": zk_servers,
                    "KAFKA_OPTS": "-Dzookeeper.4lw.commands.whitelist=ruok,srvr,mntr,stat",
                    "ZOOKEEPER_ADMIN_ENABLE_SERVER": "false",
                    "ZOOKEEPER_QUORUM_LISTEN_ON_ALL_IPS": "true",
                    "ZOOKEEPER_AUTOPURGE_SNAP_RETAIN_COUNT": "3",
                    "ZOOKEEPER_AUTOPURGE_PURGE_INTERVAL": "1",
                    "KAFKA_HEAP_OPTS": "-Xms128m -Xmx256m",
                },
                "volumes": [f"zk{i}-data:/var/lib/zookeeper/data:U",
                            f"zk{i}-log:/var/lib/zookeeper/log:U"],
                "networks": ["labnet"], "stop_grace_period": "20s",
            }
            volumes[f"zk{i}-data"] = {"name": f"{a.lab_name}-zk{i}-data", "labels": labels()}
            volumes[f"zk{i}-log"] = {"name": f"{a.lab_name}-zk{i}-log", "labels": labels()}

    voters = ",".join(f"{i}@kafka{i}:9094" for i in range(1, n + 1))
    for i in range(1, n + 1):
        external = external_port(i)
        env = {
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT",
            "KAFKA_LISTENERS": "INTERNAL://0.0.0.0:9092,EXTERNAL://0.0.0.0:9093",
            "KAFKA_ADVERTISED_LISTENERS": f"INTERNAL://kafka{i}:9092,EXTERNAL://{a.advertised_host}:{external}",
            "KAFKA_INTER_BROKER_LISTENER_NAME": "INTERNAL", "KAFKA_LOG_DIRS": "/var/lib/kafka/data",
            "KAFKA_NUM_PARTITIONS": "6", "KAFKA_DEFAULT_REPLICATION_FACTOR": str(rf),
            "KAFKA_MIN_INSYNC_REPLICAS": str(min_isr), "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": str(rf),
            "KAFKA_OFFSETS_TOPIC_NUM_PARTITIONS": "6", "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": str(rf),
            "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": str(min_isr), "KAFKA_TRANSACTION_STATE_LOG_NUM_PARTITIONS": "3",
            "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "1000", "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
            "KAFKA_DELETE_TOPIC_ENABLE": "true", "KAFKA_UNCLEAN_LEADER_ELECTION_ENABLE": "false",
            "KAFKA_AUTO_LEADER_REBALANCE_ENABLE": "false", "KAFKA_REPLICA_LAG_TIME_MAX_MS": "10000",
            "KAFKA_LOG_RETENTION_HOURS": "24", "KAFKA_LOG_SEGMENT_BYTES": "16777216",
            "KAFKA_LOG_RETENTION_CHECK_INTERVAL_MS": "10000", "KAFKA_LOG_CLEANER_ENABLE": "true",
            "KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m",
        }
        if a.mode == "zk":
            env.update({"KAFKA_BROKER_ID": str(i), "KAFKA_ZOOKEEPER_CONNECT": zk_connect,
                        "KAFKA_ZOOKEEPER_SESSION_TIMEOUT_MS": "12000"})
            volume_name = f"kafka{i}-data"
        else:
            env.update({"KAFKA_NODE_ID": str(i), "KAFKA_PROCESS_ROLES": "broker,controller",
                        "KAFKA_CONTROLLER_QUORUM_VOTERS": voters, "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
                        "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT",
                        "KAFKA_LISTENERS": "INTERNAL://0.0.0.0:9092,CONTROLLER://0.0.0.0:9094,EXTERNAL://0.0.0.0:9093",
                        "CLUSTER_ID": a.cluster_id})
            volume_name = f"kraft-kafka{i}-data"
        services[f"kafka{i}"] = {
            "image": f"docker.io/confluentinc/cp-kafka:{a.cp_version}",
            "container_name": f"{a.lab_name}-kafka{i}", "hostname": f"kafka{i}",
            "restart": "no", "labels": labels(), "environment": env,
            "ports": [f"{a.bind_ip}:{external}:9093"],
            "volumes": [f"{volume_name}:/var/lib/kafka/data:U"], "networks": ["labnet"],
            "stop_grace_period": "30s", "ulimits": {"nofile": {"soft": 65536, "hard": 65536}},
        }
        volumes[volume_name] = {"name": f"{a.lab_name}-{volume_name}", "labels": labels()}

    services["tools"] = {
        "image": f"localhost/{a.lab_name}-tools:1.0",
        "build": {"context": "./client", "dockerfile": "Containerfile"},
        "container_name": f"{a.lab_name}-tools", "restart": "no", "labels": labels(),
        "environment": {"BOOTSTRAP_SERVERS": ",".join(brokers),
                        "BROKER_COUNT": str(n), "REPLICATION_FACTOR": str(rf)},
        "networks": ["labnet"],
    }
    result = {"services": services, "networks": {"labnet": {
        "name": f"{a.lab_name}-net", "driver": "bridge", "labels": labels()}}, "volumes": volumes}
    a.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
