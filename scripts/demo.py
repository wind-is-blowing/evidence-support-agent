"""Local contest launcher. Secrets stay in ignored .runtime/contest, never in source.

Requires the project's Python environment, Node and Docker Desktop/Engine.
Starts API and worker on the host so local Ollama can use the same GPU.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime/contest"
CONFIG = STATE / "config.json"


def run(args, *, env=None, capture=True):
    return subprocess.run(
        args, cwd=ROOT, env=env, check=True, text=True, encoding="utf-8", capture_output=capture
    )


def inspect(name):
    return json.loads(run(["docker", "inspect", name]).stdout)[0]


def _replace_url_port(value, port):
    """Keep credentials and URL components intact while refreshing a loopback port."""
    parsed = urlsplit(value)
    userinfo, separator, _ = parsed.netloc.rpartition("@")
    host = parsed.hostname or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = (userinfo + separator if separator else "") + f"{host}:{int(port)}"
    return urlunsplit(parsed._replace(netloc=netloc))


def _refresh_saved_ports(config, database_info, redis_info=None):
    """Refresh random Docker host ports without rotating or exposing saved secrets."""
    database_port = database_info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
    for section, keys in (
        ("online", ("DATABASE_URL", "READ_DATABASE_URL", "WORK_DATABASE_URL")),
        ("maintenance", ("MIGRATION_DATABASE_URL", "IMPORT_DATABASE_URL")),
    ):
        for key in keys:
            config[section][key] = _replace_url_port(config[section][key], database_port)
    if redis_info is not None:
        redis_port = redis_info["NetworkSettings"]["Ports"]["6379/tcp"][0]["HostPort"]
        config["online"]["REDIS_URL"] = _replace_url_port(
            config["online"]["REDIS_URL"], redis_port
        )
    return config


def provision(args):
    if CONFIG.exists():
        config = json.loads(CONFIG.read_text("utf-8"))
        containers = [config["database_container"]]
        redis_name = config.get("redis_container")
        if not redis_name and containers[0].startswith("kefu-contest-pg-"):
            redis_name = containers[0].replace("kefu-contest-pg-", "kefu-contest-redis-", 1)
        if redis_name:
            containers.append(redis_name)
        for name in containers:
            info = inspect(name)
            if not info["Config"].get("Labels", {}).get("kefu.demo.run"):
                raise RuntimeError("Saved configuration does not identify a Kefu demo container")
            if not info["State"]["Running"]:
                run(["docker", "start", name])
        database_info = inspect(config["database_container"])
        redis_info = inspect(redis_name) if redis_name else None
        refreshed = _refresh_saved_ports(config, database_info, redis_info)
        CONFIG.write_text(json.dumps(refreshed, indent=2), encoding="utf-8")
        return config
    STATE.mkdir(parents=True, exist_ok=True)
    redis_name = None
    if args.database_container:
        info = inspect(args.database_container)
        if not info["Config"].get("Labels", {}).get("kefu.demo.run"):
            raise RuntimeError("Only a labeled Kefu demo container may be reused")
        values = dict(item.split("=", 1) for item in info["Config"]["Env"])
        port = info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        redis_port = args.redis_port
        container = args.database_container
    else:
        suffix = secrets.token_hex(4)
        container = f"kefu-contest-pg-{suffix}"
        values = {
            "POSTGRES_USER": "postgres",
            "POSTGRES_DB": "kefu",
            "POSTGRES_PASSWORD": secrets.token_hex(24),
        }
        for role in ("EXEC", "READ", "WORK", "MIGRATE", "IMPORT"):
            values[f"KEFU_{role}_PASSWORD"] = secrets.token_hex(24)
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--label",
            f"kefu.demo.run={suffix}",
            "-p",
            "127.0.0.1::5432",
            "--mount",
            f"type=bind,source={ROOT / 'deploy/init-roles.sh'},target=/docker-entrypoint-initdb.d/10-kefu-roles.sh,readonly",
        ]
        for key in values:
            cmd.extend(["-e", key])
        run(cmd + ["pgvector/pgvector:pg17"], env={**os.environ, **values})
        info = inspect(container)
        port = info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        redis_name = f"kefu-contest-redis-{suffix}"
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                redis_name,
                "--label",
                f"kefu.demo.run={suffix}",
                "-p",
                "127.0.0.1::6379",
                "redis:7.4.11-alpine",
            ]
        )
        redis_port = inspect(redis_name)["NetworkSettings"]["Ports"]["6379/tcp"][0]["HostPort"]

    def dsn(role):
        return f"postgresql+psycopg://kefu_{role}:{quote(values[f'KEFU_{role.upper()}_PASSWORD'], safe='')}@127.0.0.1:{port}/{values['POSTGRES_DB']}"

    config = {
        "database_container": container,
        "redis_container": redis_name,
        "port": args.port,
        "online": {
            "DATABASE_URL": dsn("exec"),
            "READ_DATABASE_URL": dsn("read"),
            "WORK_DATABASE_URL": dsn("work"),
            "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
            "CONTEXT_CURSOR_SECRET": secrets.token_hex(32),
        },
        "maintenance": {
            "MIGRATION_DATABASE_URL": dsn("migrate"),
            "IMPORT_DATABASE_URL": dsn("import"),
        },
    }
    CONFIG.write_text(json.dumps(config, indent=2), encoding="utf-8")
    if os.name != "nt":
        CONFIG.chmod(0o600)
    return config


def environment(config, model):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"MIGRATION_DATABASE_URL", "IMPORT_DATABASE_URL"}
    }
    env.update(config["online"])
    env.update(
        ENVIRONMENT="local",
        SIMULATED_BUSINESS="true",
        SECURE_COOKIES="false",
        APP_ORIGIN=f"http://localhost:{config['port']}",
        BIND_HOST="127.0.0.1",
        PORT=str(config["port"]),
        MODEL_MODE="demo" if model == "demo" else "qwen",
        MODEL_PROVIDER="ollama",
        MODEL_NAME=model,
        MODEL_API_KEY="ollama",
        MODEL_BASE_URL="http://127.0.0.1:11434/v1",
        MATERIALS_ROOT=str(STATE / "materials"),
        CONTEXT_ROOT=str(STATE / "context"),
        DELETION_LEDGER_PATH=str(STATE / "deletions.jsonl"),
        PYTHONIOENCODING="utf-8",
    )
    return env


def health(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=2) as response:
            return json.load(response)
    except Exception:
        return None


def stop():
    record = STATE / "processes.json"
    if not record.exists():
        return
    for pid in json.loads(record.read_text("utf-8")).values():
        if os.name == "nt":
            command = run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine",
                ]
            ).stdout
            if str(ROOT).casefold() in command.casefold() and "scripts/demo.py" in command.replace(
                "\\", "/"
            ):
                run(["taskkill", "/PID", str(pid), "/T", "/F"])
        else:
            proc = Path(f"/proc/{int(pid)}/cmdline")
            if (
                proc.exists()
                and str(ROOT).encode() in proc.read_bytes()
                and b"scripts/demo.py" in proc.read_bytes()
            ):
                os.kill(pid, 15)
    print("Local app processes stopped; database and files preserved.")


def start(args):
    config = provision(args)
    if health(config["port"]):
        print(
            f"Already running at http://localhost:{config['port']}; use stop before changing model."
        )
        return
    env = environment(config, args.model)
    if args.model != "demo":
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as response:
            names = {m["name"] for m in json.load(response)["models"]}
        if args.model not in names:
            raise RuntimeError(
                f"Ollama model {args.model} is not installed; select an installed model or use --model demo"
            )
    for _ in range(40):
        ready = subprocess.run(
            ["docker", "exec", config["database_container"], "pg_isready", "-U", "postgres"],
            capture_output=True,
        )
        if ready.returncode == 0:
            break
        time.sleep(1)
    maintenance = {**env, **config["maintenance"], "MODEL_MODE": "demo"}
    run([sys.executable, "-m", "alembic", "upgrade", "head"], env=maintenance)
    seeded = json.loads(
        run([sys.executable, "-m", "kefu.cli", "seed-demo"], env=maintenance).stdout
    )
    if seeded.get("created_accounts"):
        (STATE / "accounts.json").write_text(
            json.dumps(seeded["created_accounts"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    if not (ROOT / "web/node_modules").exists():
        run([npm, "--prefix", "web", "ci"], capture=False)
    run([npm, "--prefix", "web", "run", "build"], capture=False)
    pids = {}
    for kind in ("api", "worker"):
        with (STATE / f"{kind}.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "_" + kind],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            pids[kind] = process.pid
    (STATE / "processes.json").write_text(json.dumps(pids), encoding="utf-8")
    for _ in range(40):
        if health(config["port"]):
            print(
                f"Ready: http://localhost:{config['port']} | model={args.model} | simulated business"
            )
            print("Demo account credentials, if newly created: .runtime/contest/accounts.json")
            return
        time.sleep(1)
    raise RuntimeError("App did not become ready. Check .runtime/contest/api.log and worker.log")


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "stop", "status", "assign", "_api", "_worker"])
    parser.add_argument("--model", default="demo")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--database-container")
    parser.add_argument("--redis-port", type=int, default=7400)
    parser.add_argument("--task-id")
    parser.add_argument("--staff", default="demo-staff")
    args = parser.parse_args()
    if args.command.startswith("_"):
        import runpy

        runpy.run_module(
            "kefu.api" if args.command == "_api" else "kefu.worker", run_name="__main__"
        )
    elif args.command == "start":
        start(args)
    elif args.command == "stop":
        stop()
    elif args.command == "assign":
        if not args.task_id:
            parser.error("assign requires --task-id")
        config = json.loads(CONFIG.read_text("utf-8"))
        env = {**environment(config, "demo"), **config["maintenance"]}
        run(
            [sys.executable, "-m", "kefu.cli", "assign-task", args.task_id, "--staff", args.staff],
            env=env,
            capture=False,
        )
    else:
        config = json.loads(CONFIG.read_text("utf-8")) if CONFIG.exists() else {"port": args.port}
        print(json.dumps(health(config["port"]) or {"status": "not_running"}))
