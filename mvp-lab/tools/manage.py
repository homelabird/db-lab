#!/usr/bin/env python3
"""Isolated, non-destructive lifecycle and deliberately scoped experiments."""
from __future__ import annotations
import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SERVICES = ("mariadb", "kafka", "elasticsearch", "redis", "api", "worker", "redis-spare")
MODES = ("normal", "redis-spare", "bad-db-password", "fresh-search")
SECRET_KEYS = ("SQL_ROOT_PASSWORD", "SQL_PASSWORD", "REDIS_PASSWORD")


def checked_path(path):
    """Reject existing and dangling symlinks, including redirected parent dirs."""
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Symlinked MVP configuration/state/report path is refused")
    return path


def check_storage():
    for name in (".env", ".state", "reports", ".state/manage.lock",
                 ".state/identity.json", ".state/engine-target.json", ".state/experiment.json",
                 ".state/simulation-active.json", ".state/drill-active.json", ".state/message-active.json"):
        checked_path(ROOT / name)
    for name in ("simulations", "drills", "messages", "studies", "acceptance", "share", "startup"):
        checked_path(ROOT / "reports" / name)


def parse_env(path):
    path = checked_path(path)
    result = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        if "=" not in raw:
            raise ValueError(f"Malformed .env line {number}")
        key, value = raw.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise ValueError(f"Invalid/duplicate .env key on line {number}")
        words = shlex.split(value, comments=True)
        if len(words) != 1 or "$" in value or "`" in value:
            raise ValueError(f"Only literal single values are allowed on line {number}")
        result[key] = words[0]
    return result


def validate(config):
    required = set(parse_env(ROOT / ".env.example"))
    if set(config) != required:
        raise ValueError(".env keys must match .env.example (no secrets are printed)")
    if not re.fullmatch(r"db-lab-mvp(?:-[a-z0-9-]{1,24})?", config["MVP_PROJECT"]):
        raise ValueError("MVP_PROJECT must be db-lab-mvp or db-lab-mvp-<suffix>")
    if config["MVP_ENGINE"] not in {"auto", "docker", "podman"}:
        raise ValueError("MVP_ENGINE must be auto, docker or podman")
    if not 1024 <= int(config["API_PORT"]) <= 65535:
        raise ValueError("Invalid API_PORT")
    if not 1 <= int(config["CACHE_TTL"]) <= 300:
        raise ValueError("CACHE_TTL must be 1..300")
    for key in SECRET_KEYS:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", config[key]):
            raise ValueError(f"{key} must be 16..128 literal alphanumeric/_/- characters; run init")
    cid = config["KRAFT_CLUSTER_ID"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{22}", cid) or len(base64.urlsafe_b64decode(cid + "==")) != 16:
        raise ValueError("Invalid saved KRaft ID; do not regenerate for existing data")
    for key in ("MARIADB_IMAGE", "KAFKA_IMAGE", "ES_IMAGE", "REDIS_IMAGE"):
        if not re.fullmatch(r"[A-Za-z0-9./_:@-]+", config[key]) or ":" not in config[key]:
            raise ValueError(f"{key} requires an explicit image tag or digest")
    return config


def init():
    target = checked_path(ROOT / ".env")
    if target.exists():
        validate(parse_env(target))
        print("Existing MVP .env preserved; no password/cluster ID changes.")
        return
    text = (ROOT / ".env.example").read_text()
    for key in SECRET_KEYS:
        text = text.replace(key + "=GENERATE", key + "=" + secrets.token_hex(24))
    cid = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=")
    text = text.replace("KRAFT_CLUSTER_ID=GENERATE", "KRAFT_CLUSTER_ID=" + cid)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
    print("Created private MVP .env. Existing four DB labs were not changed.")


def atomic_json(path, data):
    path = checked_path(path)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        checked_path(path)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def lock():
    import fcntl
    check_storage()
    state = checked_path(ROOT / ".state")
    state.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(checked_path(state / "manage.lock"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another MVP lifecycle operation is active") from exc
        yield


def require_no_active_fault():
    for file, command in (("simulation-active.json", "simulate"), ("drill-active.json", "drills"), ("message-active.json", "messages")):
        if (ROOT / ".state" / file).exists():
            raise RuntimeError("Unfinished fault: use mvp " + command + " recover --yes first")


def mode():
    path = checked_path(ROOT / ".state" / "experiment.json")
    chosen = json.loads(path.read_text())["mode"] if path.exists() else "normal"
    if chosen not in MODES:
        raise ValueError("Unknown saved experiment")
    return chosen


def environment(config, chosen=None, inherited=None):
    chosen = chosen or mode()
    overlay = {"CACHE_HOST": "redis", "SEARCH_INDEX": "mvp-orders-v1", "APP_SQL_PASSWORD": config["SQL_PASSWORD"]}
    if chosen == "redis-spare":
        overlay["CACHE_HOST"] = "redis-spare"
    elif chosen == "bad-db-password":
        overlay["APP_SQL_PASSWORD"] = "deliberately-wrong-lab-password"
    elif chosen == "fresh-search":
        overlay["SEARCH_INDEX"] = "mvp-orders-v2"
    return {**(dict(os.environ) if inherited is None else inherited), **config, **overlay}


def provider(config):
    choice = config["MVP_ENGINE"]
    if choice in {"auto", "docker"} and shutil.which("docker"):
        if subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=10).returncode == 0:
            return ["docker", "compose"], ["docker"]
    if choice in {"auto", "podman"} and shutil.which("podman"):
        if shutil.which("podman-compose"):
            return ["podman-compose"], ["podman"]
        if subprocess.run(["podman", "compose", "version"], capture_output=True, timeout=10).returncode == 0:
            return ["podman", "compose"], ["podman"]
    raise RuntimeError("Docker Compose v2 or Podman + a Compose provider is required; no packages are auto-installed")


class Compose:
    def __init__(self, config):
        self.config = config
        self.inherited = dict(os.environ)
        self.cmd, self.engine = provider(config)
        self.base = [*self.cmd, "--env-file", str(ROOT / ".env"), "-p", config["MVP_PROJECT"],
                     "-f", str(ROOT / "compose.yaml")]

    def run(self, *args, capture=False, check=True, timeout=None, profile=False):
        command = self.base + (["--profile", "spare"] if profile else []) + list(args)
        return subprocess.run(command, cwd=ROOT, env=environment(self.config, inherited=self.inherited), check=check,
                              capture_output=capture, text=True, timeout=timeout)

    def ready(self):
        subprocess.run(self.engine + ["info"], env=self.inherited, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, check=True, timeout=20)
        # Validate real provider parsing but discard its potentially secret-bearing YAML.
        result = self.run("config", capture=True, check=False, timeout=20)
        if result.returncode:
            raise RuntimeError("Compose configuration rejected; inspect provider/version or .env syntax locally")

    def _resolved_docker_host(self):
        """Resolve the endpoint the docker CLI would use when DOCKER_HOST is unset."""
        cached = getattr(self, "_resolved_host", None)
        if cached is not None:
            return cached
        context = self.inherited.get("DOCKER_CONTEXT")
        try:
            result = subprocess.run([*self.engine, "context", "inspect", *([context] if context else [])],
                                    env=self.inherited, capture_output=True, text=True, check=True, timeout=15)
            host = json.loads(result.stdout)[0]["Endpoints"]["docker"]["Host"]
        except (subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
            host = ""
        self._resolved_host = host
        return host

    def engine_selector(self):
        keys = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                "CONTAINER_HOST", "CONTAINER_CONNECTION", "CONTAINERS_CONF")
        data = {k: self.inherited.get(k, "") for k in keys}
        data["engine"] = self.engine
        # An unset DOCKER_HOST selects the current context's default endpoint.
        # Normalize it so the same local engine hashes identically whether it was
        # selected via DOCKER_HOST or via the default context (e.g. interactive
        # shells that export DOCKER_HOST=unix:///var/run/docker.sock).
        if self.engine == ["docker"] and not data["DOCKER_HOST"]:
            data["DOCKER_HOST"] = self._resolved_docker_host()
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def engine_identity(self):
        """Only record a digest of endpoint/daemon identity, never a raw remote address."""
        def read(*args):
            result = subprocess.run([*self.engine, *args], env=self.inherited, capture_output=True,
                                    text=True, check=True, timeout=15)
            return json.loads(result.stdout)
        info = read("info", "--format", "{{json .}}")
        if self.engine == ["docker"]:
            context = self.inherited.get("DOCKER_CONTEXT")
            host = self.inherited.get("DOCKER_HOST")
            if context or not host:
                rows = read("context", "inspect", *([context] if context else []))
                host = rows[0]["Endpoints"]["docker"]["Host"]
                self._resolved_host = host
            daemon = info.get("ID")
            if not daemon:
                raise RuntimeError("Docker did not return a daemon ID; target cannot be verified")
            fingerprint = {"endpoint": host, "daemon_id": daemon, "selector": self.engine_selector()}
            local = str(host).startswith(("unix://", "npipe://"))
        else:
            # Podman has no Docker-like daemon identity. A host/store fingerprint is weaker.
            host = info.get("host", {})
            fingerprint = {"hostname": host.get("hostname"), "store": info.get("store", {}).get("graphRoot"),
                           "selector": self.engine_selector()}
            if not fingerprint["hostname"] or not fingerprint["store"]:
                raise RuntimeError("Podman host/store target could not be verified")
            local = False  # automated live simulation currently requires local Docker.
        return {"fingerprint": hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest(),
                "engine": self.engine[0], "local_docker": self.engine == ["docker"] and local}

    def guard_engine(self, bind=False):
        identity = self.engine_identity()
        path = ROOT / ".state/engine-target.json"
        if path.exists():
            if json.loads(path.read_text()) != identity:
                raise RuntimeError("Container engine target changed; restore the original context/endpoint. No automatic rebind.")
        elif bind:
            atomic_json(path, identity)
        else:
            raise RuntimeError("Unbound engine target. Inspect the selected engine, then run mvp bind-target --yes once.")
        return identity

    def guard_target(self):
        # Password changes need not prevent a safe stop, but another project must
        # never be silently selected for down/stop after editing .env.
        path = ROOT / ".state" / "identity.json"
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get("_engine_selector", self.engine_selector()) != self.engine_selector():
                raise RuntimeError("Engine selector changed; restore the original environment")
            current = hashlib.sha256(self.config["MVP_PROJECT"].encode()).hexdigest()
            if previous.get("MVP_PROJECT") != current:
                raise RuntimeError("MVP project target changed; restore the original .env before lifecycle operations")

    def guard_identity(self):
        path = ROOT / ".state" / "identity.json"
        guarded = {key: hashlib.sha256(self.config[key].encode()).hexdigest()
                   for key in (*SECRET_KEYS, "KRAFT_CLUSTER_ID", "MVP_PROJECT")}
        guarded["_engine_selector"] = self.engine_selector()
        if path.exists() and json.loads(path.read_text()) != guarded:
            raise RuntimeError("Saved project/credentials/cluster ID changed. Restore .env; no automatic secret rotation or volume reset")
        if not path.exists():
            atomic_json(path, guarded)

    def wait_initialized(self, seconds=180):
        from tools.startup import collect, log_signals
        if type(seconds) not in (int, float) or not 0 < seconds <= 600:
            raise ValueError("Initialization budget must be 0..600 seconds")
        deadline = time.monotonic() + seconds
        initialized = False
        docker_readiness = self.engine == ["docker"] and bool(self.guard_engine().get("local_docker"))
        work_deadline = deadline - min(10, seconds / 4) if docker_readiness else deadline
        evidence = {"schema": 1, "status": "waiting", "admin_initialized": False,
                    "attempts": 0, "last_error": None, "admin_signals": [], "containers": []}
        directory = ROOT / "reports/startup"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / ("startup-" + uuid.uuid4().hex[:12] + ".json")
        while time.monotonic() < work_deadline:
            evidence["attempts"] += 1
            if not initialized:
                try:
                    remaining = deadline - time.monotonic()
                    # Keep time to record container states if admin init stalls.
                    init_timeout = min(40, max(.01, remaining - min(5, remaining / 2)))
                    result = self.run("exec", "-T", "api", "python", "-m", "mvp_app.admin", "init",
                                      capture=True, check=False, timeout=init_timeout)
                    initialized = result.returncode == 0
                    evidence["last_error"] = None if initialized else "admin_initialization_failed"
                    if not initialized:
                        evidence["admin_signals"] = sorted(set(evidence["admin_signals"] +
                            log_signals((getattr(result, "stdout", "") or "") + "\n" +
                                        (getattr(result, "stderr", "") or ""))))
                except subprocess.TimeoutExpired as exc:
                    evidence["last_error"] = "admin_initialization_timeout"
                    output = "\n".join(str(part) for part in (exc.stdout, exc.stderr) if part)
                    evidence["admin_signals"] = sorted(set(evidence["admin_signals"] + log_signals(output)))
                evidence["admin_initialized"] = initialized
            left = deadline - time.monotonic()
            if left > 0:
                if docker_readiness:
                    observed = collect(self, include_logs=False, budget=min(15, left))
                    evidence["containers"] = observed["containers"]
                    ready = initialized and observed["ready"]
                else:
                    # Legacy non-local/Podman lifecycle stays available; not locally certified readiness.
                    evidence["readiness_scope"] = "admin_only_non_local_docker_not_runtime_certified"
                    ready = initialized
                if ready:
                    evidence["status"] = "initialized_and_ready" if docker_readiness else "initialized"
                    atomic_json(path, evidence)
                    print("Initialization evidence:", str(path))
                    return
            atomic_json(path, evidence)
            left = deadline - time.monotonic()
            if left > 0:
                time.sleep(min(3, left))
        if docker_readiness:
            left = deadline - time.monotonic()
            if left > 0:
                try:
                    observed = collect(self, include_logs=True, log_services=("api", "worker"),
                                       budget=min(10, left))
                    evidence["containers"] = observed["containers"]
                    if initialized and observed["ready"]:
                        evidence["status"] = "initialized_and_ready"
                        atomic_json(path, evidence)
                        print("Initialization evidence:", str(path))
                        return
                except Exception:
                    evidence["diagnostics"] = "container_observation_failed"
        evidence["status"] = "failed"
        atomic_json(path, evidence)
        raise RuntimeError("Initialization/readiness deadline exceeded; containers and volumes retained. "
                           "Inspect ./all.sh mvp logs api, ./all.sh mvp logs worker, and "
                           "./all.sh mvp diagnose. Sanitized evidence: " + str(path))


def parser():
    p = argparse.ArgumentParser(description="Small standalone MVP; no production data, automatic deletion or host firewall changes.")
    sub = p.add_subparsers(dest="action")
    bind = sub.add_parser("bind-target", help="Pin the already selected engine; never overwrites an existing pin")
    bind.add_argument("--yes", action="store_true", required=True)
    try:
        from tools.simulation import add_parser as simulation_parser
    except ModuleNotFoundError:
        from simulation import add_parser as simulation_parser
    simulation_parser(sub)
    from tools.advanced import add_parser as advanced_parser
    advanced_parser(sub)
    from tools.messages import add_parser as messages_parser
    messages_parser(sub)
    from tools.acceptance import add_parser as acceptance_parser
    acceptance_parser(sub)
    from tools.study import add_parser as study_parser
    study_parser(sub)
    for name in ("init", "up", "down", "status", "doctor", "diagnose", "smoke", "test", "inspect-runtime"):
        sub.add_parser(name)
    logs = sub.add_parser("logs")
    logs.add_argument("service", nargs="?", choices=SERVICES)
    logs.add_argument("--follow", action="store_true")
    for name in ("stop", "resume", "recreate"):
        s = sub.add_parser(name)
        s.add_argument("service", choices=SERVICES)
        if name != "resume":
            s.add_argument("--yes", action="store_true", required=True)
    exp = sub.add_parser("experiment")
    exp.add_argument("mode", choices=MODES)
    exp.add_argument("--yes", action="store_true", required=True)
    rebuild = sub.add_parser("rebuild-search")
    rebuild.add_argument("--yes", action="store_true", required=True)
    sql = sub.add_parser("sql")
    sql.add_argument("query", help="Named diagnostics only: orders, outbox, counts or ping")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not args.action:
        p.print_help()
        return 0
    try:
        check_storage()
        if args.action == "study":
            from tools.study import cli
            return cli(args, sys.modules[__name__])
        if args.action == "verify":
            from tools.acceptance import cli
            return cli(args, sys.modules[__name__])
        if args.action == "messages":
            from tools.messages import cli
            return cli(args, sys.modules[__name__])
        if args.action == "drills":
            from tools.advanced import cli
            return cli(args, sys.modules[__name__])
        if args.action == "simulate":
            try:
                from tools.simulation import cli
            except ModuleNotFoundError:
                from simulation import cli
            return cli(args, sys.modules[__name__])
        if args.action == "test":
            return subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT).returncode
        if args.action == "init":
            with lock():
                init()
            return 0
        mutations = {"up", "down", "stop", "resume", "recreate", "experiment", "rebuild-search"}
        if args.action in mutations:
            require_no_active_fault()
            if (ROOT / ".state/simulation-active.json").exists():
                raise RuntimeError("Unfinished simulation: use mvp simulate recover --yes first")
            if (ROOT / ".state/drill-active.json").exists():
                raise RuntimeError("Unfinished disposable drill: use mvp drills recover --yes first")
        if args.action == "up":
            with lock():
                require_no_active_fault()
                init()
                config = validate(parse_env(ROOT / ".env"))
                compose = Compose(config)
                compose.ready(); compose.guard_target()
                compose.guard_engine(bind=not (ROOT / ".state/identity.json").exists())
                compose.guard_identity()
                print("MVP target:", config["MVP_PROJECT"], "experiment:", mode())
                # A deliberate wrong API password should not look like successful startup.
                if mode() == "bad-db-password":
                    raise RuntimeError("Wrong-password experiment active. Restore with experiment normal --yes first")
                if mode() == "redis-spare":
                    compose.run("up", "-d", "redis-spare", profile=True)
                compose.run("up", "-d", "--build")
                compose.wait_initialized()
                print("Open http://127.0.0.1:" + config["API_PORT"] + " ; run smoke to verify end-to-end delivery.")
            return 0
        if not (ROOT / ".env").exists():
            raise RuntimeError("Run ./all.sh mvp init first")
        config = validate(parse_env(ROOT / ".env"))
        compose = Compose(config)
        if args.action in {"diagnose", "smoke"}:
            from tools.http_target import guard, diagnostics
            if args.action == "diagnose":
                guard(compose)
                result = diagnostics(config)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["dependencies_reachable"] else 1
            # smoke writes a synthetic order: serialize with lifecycle/fault operations.
            with lock():
                require_no_active_fault()
                guard(compose)
                return subprocess.run([sys.executable, str(ROOT / "tools/probe.py"), "--url",
                                       "http://127.0.0.1:" + config["API_PORT"],
                                       "--project", config["MVP_PROJECT"]], cwd=ROOT, timeout=90).returncode
        if args.action == "inspect-runtime":
            from tools.startup import collect
            result = collect(compose)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ready"] else 1
        if args.action == "bind-target":
            with lock():
                compose.guard_target()
                # Existing identities from v1 may only lack the new selector digest.
                path = ROOT / ".state/identity.json"
                if path.exists():
                    previous = json.loads(path.read_text())
                    for key in (*SECRET_KEYS, "KRAFT_CLUSTER_ID", "MVP_PROJECT"):
                        if previous.get(key) != hashlib.sha256(config[key].encode()).hexdigest():
                            raise RuntimeError("Existing credential/project identity differs; no migration applied")
                compose.guard_engine(bind=True)
                if path.exists():
                    if "_engine_selector" not in previous:
                        atomic_json(path, {**previous, "_engine_selector": compose.engine_selector()})
                compose.guard_identity()
                print("Engine target pinned without changing containers, volumes or credentials.")
        elif args.action == "doctor":
            compose.ready()
            print("Engine and Compose config OK. This is not an image-pull, capacity or DB health certification.")
            print("Only API loopback port", config["API_PORT"], "is published. No DB host ports.")
        elif args.action == "status":
            print("Experiment:", mode())
            compose.run("ps", "-a", profile=True)
        elif args.action == "logs":
            options = ["logs", "--tail", "150"] + (["-f"] if args.follow else [])
            if args.service:
                options.append(args.service)
            compose.run(*options, profile=True)
        elif args.action == "sql":
            queries = {
                "orders": "SELECT id,item,status,version,created_at FROM orders ORDER BY created_at DESC LIMIT 20",
                "outbox": "SELECT seq,event_id,created_at,sent_at FROM outbox ORDER BY seq DESC LIMIT 20",
                "counts": "SELECT (SELECT COUNT(*) FROM orders) AS orders, (SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL) AS pending",
                "ping": "SELECT 1"}
            if args.query not in queries:
                raise ValueError("Arbitrary SQL is disabled. Use sql orders|outbox|counts|ping")
            q = queries[args.query]
            compose.guard_target(); compose.guard_engine()
            compose.run("exec", "-T", "mariadb", "sh", "-ec",
                        'export MYSQL_PWD="$MARIADB_PASSWORD"; exec mariadb --user="$MARIADB_USER" --database=mvp --execute="$1"',
                        "mvp-sql", q)
        else:
            with lock():
                require_no_active_fault()
                # down/stop preserves volumes; still refuse an accidental project switch.
                compose.guard_target(); compose.guard_engine()
                if args.action not in {"down", "stop"}:
                    compose.guard_identity()
                if args.action == "down":
                    compose.run("down", profile=True)
                    print("Containers/network stopped; all named data volumes preserved.")
                elif args.action == "stop":
                    compose.run("stop", args.service, profile=args.service == "redis-spare")
                elif args.action in {"resume", "recreate"}:
                    opts = ["up", "-d", "--no-deps"]
                    if args.action == "recreate":
                        opts.append("--force-recreate")
                    compose.run(*opts, args.service, profile=args.service == "redis-spare")
                    print("Same service and volume reused. Inspect status and run smoke; no data was copied or deleted.")
                elif args.action == "experiment":
                    if args.mode == "redis-spare":
                        compose.run("up", "-d", "redis-spare", profile=True)
                    atomic_json(ROOT / ".state/experiment.json", {"mode": args.mode})
                    print("Desired experiment:", args.mode, "(persists; only one mode active)")
                    compose.run("up", "-d", "--no-deps", "--force-recreate", "api", "worker")
                    if args.mode == "fresh-search":
                        # Create only; do not rewind the existing consumer group's offsets.
                        compose.run("exec", "-T", "api", "python", "-c",
                                    'from mvp_app.adapters import Search,Settings; Search(Settings.load()).initialize()')
                    print("Experiment applied. Inspect diagnostics; normal restores app config, not stopped DB services.")
                elif args.action == "rebuild-search":
                    compose.run("exec", "-T", "api", "python", "-m", "mvp_app.admin", "rebuild-search")
        return 0
    except KeyboardInterrupt:
        print("Interrupted; existing containers and data retained.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # CalledProcessError's str may contain SQL arguments; do not echo its command.
        detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print("MVP ERROR:", detail, file=sys.stderr)
        print("No automatic volume deletion or rollback was performed.", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
