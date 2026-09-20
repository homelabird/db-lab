"""Bounded, labelled local-Docker faults. No volume deletion, generic shell or remote target."""
from __future__ import annotations
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid

BASE_SERVICES = ("mariadb", "kafka", "elasticsearch", "redis", "api", "worker")
ALLOWED = {"stop": {"redis", "kafka", "elasticsearch"},
           "pause": {"mariadb", "worker"}, "recreate": {"redis"},
           "switch": {"redis"}, "row-lock": {"api"},
           "netem-delay": {"mariadb"}, "netem-loss": {"mariadb"}}


class DockerLab:
    def __init__(self, compose, manage):
        self.c, self.m = compose, manage
        self.active_path = manage.ROOT / ".state/simulation-active.json"
        self.helper = None

    def binding(self):
        self.c.guard_target()
        value = self.c.guard_engine()
        if not value["local_docker"] or self.c.engine != ["docker"]:
            raise RuntimeError("Automated simulation currently requires a pinned local Docker engine (unix/npipe)")
        return value

    def docker(self, *args, timeout=30):
        self.binding()
        result = subprocess.run([*self.c.engine, *args], env=self.c.inherited,
                                capture_output=True, text=True, check=True, timeout=timeout)
        return result.stdout

    def inspect(self, container_id, service):
        if not re.fullmatch(r"[a-f0-9]{12,64}", container_id):
            raise RuntimeError("Invalid container identifier in simulation state")
        rows = json.loads(self.docker("inspect", container_id))
        if len(rows) != 1:
            raise RuntimeError("Expected exactly one labelled container")
        row = rows[0]
        labels = row["Config"].get("Labels", {})
        if (labels.get("com.docker.compose.project") != self.c.config["MVP_PROJECT"]
                or labels.get("com.docker.compose.service") != service):
            raise RuntimeError("Container ownership labels do not match; refusing fault/recovery")
        return {"id": row["Id"], "service": service, "image_id": row["Image"],
                "image_reference": row["Config"]["Image"],
                "running": bool(row["State"]["Running"]), "paused": bool(row["State"].get("Paused")),
                "health": row["State"].get("Health", {}).get("Status"),
                "oom_killed": bool(row["State"].get("OOMKilled")),
                "restart_count": row.get("RestartCount", 0),
                "published_ports": row.get("NetworkSettings", {}).get("Ports", {}),
                "volumes": {m["Destination"]: m.get("Name", "") for m in row.get("Mounts", []) if m["Type"] == "volume"}}

    def service(self, name, required=True):
        self.binding()
        result = self.c.run("ps", "-a", "-q", name, capture=True, timeout=20, profile=name == "redis-spare")
        ids = result.stdout.split()
        if not ids and not required:
            return None
        if len(ids) != 1:
            raise RuntimeError("Simulation requires exactly one container per service: " + name)
        return self.inspect(ids[0], name)

    def preflight(self):
        if (self.m.ROOT / ".state/message-active.json").exists():
            raise RuntimeError("Unfinished message drill: mvp messages recover --yes")
        self.binding(); self.c.guard_identity()
        if (self.m.ROOT / ".state/drill-active.json").exists():
            raise RuntimeError("Unfinished disposable drill exists: mvp drills recover --yes")
        if self.active_path.exists():
            raise RuntimeError("Unfinished simulation fault exists. Inspect state, then simulate recover --yes")
        if self.m.mode() != "normal":
            raise RuntimeError("Restore experiment normal before starting an automated simulation")
        states = [self.service(name) for name in BASE_SERVICES]
        api = next(s for s in states if s["service"] == "api")
        published = api.get("published_ports", {}).get("8080/tcp") or []
        expected = {"HostIp": "127.0.0.1", "HostPort": self.c.config["API_PORT"]}
        all_bindings = [(port, binding) for port, bindings in api.get("published_ports", {}).items()
                        for binding in (bindings or [])]
        if published != [expected] or all_bindings != [("8080/tcp", expected)]:
            raise RuntimeError("API must publish exactly the configured loopback port and no additional binding")
        if any(not s["running"] or s["paused"] or s["health"] not in {None, "healthy"} for s in states):
            raise RuntimeError("All six MVP containers must already be running and ready; simulation never auto-starts the stack")
        return states

    def _save(self, record):
        self.m.atomic_json(self.active_path, record)

    def _read(self):
        value = json.loads(self.active_path.read_text())
        action, service = value.get("action"), value.get("service")
        if action not in ALLOWED or service not in ALLOWED[action]:
            raise RuntimeError("Unknown recovery action; no command executed")
        if (value.get("engine") != self.binding() or value.get("project") != self.c.config["MVP_PROJECT"]
                or not re.fullmatch(r"sim-[a-f0-9]{12}", value.get("run_id", ""))):
            raise RuntimeError("Recovery state belongs to a different run/engine/project")
        return value

    def _apps(self):
        self.binding()
        self.c.run("up", "-d", "--no-deps", "--force-recreate", "--no-build", "--pull", "never",
                   "api", "worker", capture=True, timeout=65)

    def apply(self, action, service, run_id, seconds, order_id=None):
        if action not in ALLOWED or service not in ALLOWED[action] or not 1 <= seconds <= 30:
            raise ValueError("Unsupported or unbounded fault")
        if not isinstance(run_id, str) or not re.fullmatch(r"sim-[a-f0-9]{12}", run_id):
            raise ValueError("Invalid simulation run identifier")
        if action == "row-lock":
            try:
                if str(uuid.UUID(order_id)) != order_id:
                    raise ValueError("Noncanonical synthetic order ID")
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("A canonical synthetic order UUID is required") from exc
        if self.active_path.exists():
            raise RuntimeError("Another simulation fault is active")
        original = self.service(service)
        if not original["running"] or original["paused"]:
            raise RuntimeError("Fault target was not running normally")
        record = {"schema": 1, "run_id": run_id, "project": self.c.config["MVP_PROJECT"],
                  "engine": self.binding(), "action": action, "service": service,
                  "original": original, "requested_at_epoch": time.time(), "status": "requested"}
        if action == "row-lock":
            if not re.fullmatch(r"[a-f0-9-]{36}", order_id or ""):
                raise ValueError("A synthetic order is required for a row-lock drill")
            record["order_id"] = order_id
            record["max_hold_seconds"] = seconds
        if action == "switch":
            previous = self.service("redis-spare", required=False)
            record["spare_before"] = previous
        self._save(record)  # persist intent BEFORE an operation that might partially succeed
        if action in {"netem-delay", "netem-loss"}:
            from .netem import start
            record["netem_event"] = start(self, record, seconds)
        elif action in {"stop", "pause"}:
            argv = ["stop", "--time", "4", original["id"]] if action == "stop" else ["pause", original["id"]]
            self.docker(*argv)
            changed = self.inspect(original["id"], service)
            if (action == "pause" and not changed["paused"]) or (action == "stop" and changed["running"]):
                raise RuntimeError("Requested fault was not observed in container state")
            record["fault_state"] = changed
        elif action == "recreate":
            self.c.run("up", "-d", "--no-deps", "--force-recreate", "--no-build", "--pull", "never",
                       "redis", capture=True, timeout=50)
            changed = self.service("redis")
            record["replacement"] = changed
            self._save(record)
            self._same_storage_image(original, changed)
        elif action == "switch":
            self.c.run("up", "-d", "--pull", "never", "redis-spare", profile=True, capture=True, timeout=45)
            record["spare_started"] = self.service("redis-spare")
            self._save(record)
            self.m.atomic_json(self.m.ROOT / ".state/experiment.json", {"mode": "redis-spare"})
            self._apps()
        else:
            command = [*self.c.base, "exec", "-T", "api", "python", "-m", "mvp_app.lock_drill",
                       "--order-id", order_id, "--run-id", run_id, "--seconds", str(seconds)]
            self.helper = subprocess.Popen(command, cwd=self.m.ROOT,
                    env=self.m.environment(self.c.config, inherited=self.c.inherited),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            ready = queue.Queue()
            def watch():
                for line in self.helper.stdout:
                    try:
                        event = json.loads(line)
                        if event.get("stage") in {"row_lock_acquired", "row_lock_failed"}:
                            ready.put(event)
                    except (ValueError, TypeError):
                        pass
            threading.Thread(target=watch, daemon=True).start()
            try:
                event = ready.get(timeout=12)
            except queue.Empty as exc:
                raise RuntimeError("Lock acquisition was not observed; helper has a bounded hold timer") from exc
            if event["stage"] != "row_lock_acquired":
                raise RuntimeError("Synthetic row lock was rejected or could not be acquired")
            record["lock_event"] = event
        record["status"] = "applied"
        record["applied_at_epoch"] = time.time()
        self._save(record)
        return record

    @staticmethod
    def _same_storage_image(original, changed):
        if original["image_id"] != changed["image_id"] or original["volumes"] != changed["volumes"]:
            raise RuntimeError("Replacement image or volume identity differs; do not assume a safe rollback")

    def restore(self):
        if not self.active_path.exists():
            return {"restored": True, "action": "none"}
        record = self._read()
        action, service = record["action"], record["service"]
        original = record["original"]
        network_result = None
        if action in {"netem-delay", "netem-loss"}:
            from .netem import restore
            network_result = restore(self, record)
        elif action in {"stop", "pause"}:
            current = self.inspect(original["id"], service)
            if action == "pause":
                if current["paused"]:
                    self.docker("unpause", original["id"])
                if not current["running"]:
                    raise RuntimeError("Paused container was stopped externally; automatic restart refused")
            elif not current["running"]:
                self.docker("start", original["id"])
            current = self.inspect(original["id"], service)
            if not current["running"] or current["paused"]:
                raise RuntimeError("Container process restoration not observed")
        elif action == "recreate":
            current = self.service("redis")
            self._same_storage_image(original, current)
            if not current["running"]:
                self.docker("start", current["id"])
        elif action == "switch":
            if self.m.mode() not in {"normal", "redis-spare"}:
                raise RuntimeError("Experiment changed externally; restore refused")
            self.m.atomic_json(self.m.ROOT / ".state/experiment.json", {"mode": "normal"})
            self._apps()
            before, started = record.get("spare_before"), record.get("spare_started")
            if started and not (before and before["running"]):
                self.inspect(started["id"], "redis-spare")
                self.docker("stop", "--time", "4", started["id"])
        else:
            # A killed CLI need not kill docker exec. Wait for bounded helper expiry,
            # then OBSERVE release by acquiring and immediately rolling back this row.
            if self.helper is not None:
                self.helper.wait(timeout=40)
                if self.helper.stdout:
                    self.helper.stdout.close()
            deadline = time.monotonic() + 45
            while True:
                result = self.c.run("exec", "-T", "api", "python", "-m", "mvp_app.lock_drill",
                        "--order-id", record["order_id"], "--run-id", record["run_id"], "--seconds", "0",
                        capture=True, check=False, timeout=10)
                if result.returncode == 0 and '"row_lock_released"' in result.stdout:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("Row lock release could not be confirmed; recovery marker retained")
                time.sleep(1)
        self.active_path.unlink()
        return {"restored": True, "action": action, "service": service,
                "scope": "process/config restoration, not proof of data recovery",
                **({"netem": network_result} if network_result is not None else {})}
