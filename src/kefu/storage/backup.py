"""Encrypted standard tar/pg_dump backups with a separate privacy ledger."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from psycopg import sql
from sqlalchemy.engine import make_url

MAINTENANCE_LOCK = 483018390126


def scalar(cursor):
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Expected database result is missing")
    return row[0]


def connection(settings):
    if not settings.migration_database_url:
        raise ValueError("MIGRATION_DATABASE_URL is required in the maintenance process")
    parsed = make_url(settings.migration_database_url.get_secret_value())
    return {
        "host": parsed.host,
        "port": parsed.port or 5432,
        "dbname": parsed.database,
        "user": parsed.username,
        "password": parsed.password,
        "connect_timeout": 10,
    }


def pg_environment(config):
    env = os.environ.copy()
    for field, variable in (
        ("host", "PGHOST"),
        ("port", "PGPORT"),
        ("dbname", "PGDATABASE"),
        ("user", "PGUSER"),
        ("password", "PGPASSWORD"),
    ):
        if config.get(field) is not None:
            env[variable] = str(config[field])
    return env


def cipher(key_file):
    if not key_file.is_file() or key_file.is_symlink():
        raise ValueError("Provide a protected Fernet key file outside the backup directory")
    return Fernet(key_file.read_bytes().strip())


def run_pg(command, config):
    if not shutil.which(command[0]):
        raise ValueError(f"{command[0]} PostgreSQL 17 client is required in the maintenance image")
    completed = subprocess.run(
        command, env=pg_environment(config), capture_output=True, timeout=180
    )
    if completed.returncode:
        raise RuntimeError(
            f"{command[0]} failed; database remains in maintenance; inspect protected server logs"
        )


def safe_file(root, relative):
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Backup file escapes its configured root")
    cursor = path.parent
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError("Symlink directory is not allowed in backups")
        cursor = cursor.parent
    return path


@contextmanager
def maintenance_window(config):
    """Serialize maintenance commands and restore exactly the prior pause state."""
    with psycopg.connect(**config, autocommit=True) as control:
        control.execute("SELECT pg_advisory_lock(%s)", (MAINTENANCE_LOCK,))
        original = control.execute(
            "SELECT maintenance,recovery_required FROM system_control WHERE id=1"
        ).fetchone()
        if original is None or original[1]:
            raise ValueError("An incomplete recovery cannot be backed up or reopened")
        control.execute("UPDATE system_control SET maintenance=true WHERE id=1")
        deadline = time.monotonic() + 55
        while scalar(control.execute("SELECT count(*) FROM tasks WHERE lease_until>now()")):
            if time.monotonic() > deadline:
                raise ValueError("Workers have not drained; maintenance remains enabled")
            time.sleep(0.25)
        yield
        # On an exception the database deliberately remains paused. A successful
        # backup must not open service that the maintainer had already paused.
        control.execute(
            "UPDATE system_control SET maintenance=%s WHERE id=1 AND NOT recovery_required",
            (original[0],),
        )


def backup(settings, args):
    config = connection(settings)
    encryptor = cipher(args.key_file)
    if args.path.exists():
        raise ValueError("Backup output already exists")
    with maintenance_window(config):
        with tempfile.TemporaryDirectory(prefix="kefu-backup-") as temporary:
            temp = Path(temporary)
            with psycopg.connect(**config) as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                conn.execute("SET LOCAL lock_timeout='10s'")
                tables = [
                    row[0]
                    for row in conn.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
                    )
                ]
                conn.execute(
                    sql.SQL("LOCK TABLE {} IN SHARE MODE").format(
                        sql.SQL(",").join(map(sql.Identifier, tables))
                    )
                )
                snapshot = scalar(conn.execute("SELECT pg_export_snapshot()"))
                run_pg(
                    [
                        "pg_dump",
                        "--format=custom",
                        "--no-owner",
                        "--exclude-table-data=public.system_control",
                        "--snapshot=" + snapshot,
                        "--file=" + str(temp / "database.dump"),
                    ],
                    config,
                )
                files = {"database.dump": (temp / "database.dump").read_bytes()}
                for task_id, kind, record in conn.execute(
                    "SELECT task_id,kind,data FROM artifacts"
                ):
                    if kind == "upload" and record.get("status") not in ("deleted", "tombstoned"):
                        relative = record["storage_key"]
                        path = safe_file(settings.materials_root.resolve(), relative)
                        content = path.read_bytes()
                        if hashlib.sha256(content).hexdigest() != record["sha256"]:
                            raise ValueError("Original material checksum mismatch")
                        files["materials/" + relative] = content
                    elif record.get("status") == "active":
                        relative = task_id + "/" + record["filename"]
                        path = safe_file(settings.context_root.resolve(), relative)
                        content = path.read_bytes()
                        if hashlib.sha256(content).hexdigest() != record["hash"]:
                            raise ValueError("Context checksum mismatch")
                        files["context/" + relative] = content
                schema = scalar(conn.execute("SELECT version_num FROM alembic_version"))
                manifest = {
                    "schema_version": schema,
                    "application_version": "0.1.0",
                    "format": "pg_dump-custom+tar+fernet",
                    "control_state_excluded": True,
                    "files": {
                        name: hashlib.sha256(value).hexdigest() for name, value in files.items()
                    },
                    "documents": [
                        list(row)
                        for row in conn.execute("SELECT id,version FROM documents ORDER BY id")
                    ],
                }
                files["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
                if not settings.deletion_ledger_path.exists():
                    if scalar(conn.execute("SELECT count(*) FROM memory_exclusions")):
                        raise ValueError("Independent privacy ledger is missing")
                    settings.deletion_ledger_path.parent.mkdir(parents=True, exist_ok=True)
                    settings.deletion_ledger_path.touch(exist_ok=False)
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                    for name, data in files.items():
                        member = tarfile.TarInfo(name)
                        member.size, member.mode = len(data), 0o600
                        tar.addfile(member, io.BytesIO(data))
                encrypted = encryptor.encrypt(archive.getvalue())
                args.path.parent.mkdir(parents=True, exist_ok=True)
                with args.path.open("xb") as output:
                    output.write(encrypted)
                    output.flush()
                    os.fsync(output.fileno())
                return {
                    "path": str(args.path.resolve()),
                    "sha256": hashlib.sha256(encrypted).hexdigest(),
                    "file_count": len(files),
                    "schema": schema,
                    "privacy_ledger": str(settings.deletion_ledger_path.resolve()),
                    "off_host_backup_verified": False,
                }


def restore(settings, args):
    if settings.environment not in ("local", "test"):
        raise ValueError("Restore must first run in an isolated local/test environment")
    if not args.deletion_ledger.is_file() or args.deletion_ledger.is_symlink():
        raise ValueError("A separately retained privacy ledger is required before restoring")
    ledger_bytes = args.deletion_ledger.read_bytes()
    ledger = [
        json.loads(line) for line in ledger_bytes.decode("utf-8").splitlines() if line.strip()
    ]
    ledger_target = settings.deletion_ledger_path
    if ledger_target.is_symlink():
        raise ValueError("Restored deletion ledger must not be a symlink")
    if ledger_target.exists() and ledger_target.read_bytes() not in (b"", ledger_bytes):
        raise ValueError(
            "Target privacy ledger has unapplied entries; provide the complete latest ledger"
        )
    config = connection(settings)
    content = cipher(args.key_file).decrypt(args.path.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
        blobs = {}
        for member in tar.getmembers():
            path = Path(member.name)
            if (
                not member.isfile()
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or ":" in member.name
            ):
                raise ValueError("Unsafe archive member")
            if member.name in blobs:
                raise ValueError("Duplicate archive member")
            handle = tar.extractfile(member)
            if handle is None:
                raise ValueError("Archive member is not readable")
            blobs[member.name] = handle.read()
    manifest = json.loads(blobs.pop("manifest.json"))
    if set(manifest["files"]) != set(blobs) or any(
        hashlib.sha256(blobs[name]).hexdigest() != checksum
        for name, checksum in manifest["files"].items()
    ):
        raise ValueError("Backup integrity check failed")
    if manifest["schema_version"] != "0004" or not manifest.get("control_state_excluded"):
        raise ValueError("Unsupported backup schema; use the matching application release")
    with psycopg.connect(**config) as conn:
        if scalar(conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'")):
            raise ValueError(
                "Restore requires an EMPTY isolated target database; existing data will not be overwritten"
            )
    for root in (settings.materials_root, settings.context_root):
        if root.exists() and any(root.iterdir()):
            raise ValueError("Restore requires empty isolated material and context directories")
    with tempfile.TemporaryDirectory(prefix="kefu-restore-") as temporary:
        dump = Path(temporary) / "database.dump"
        dump.write_bytes(blobs.pop("database.dump"))
        run_pg(
            [
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--dbname=" + config["dbname"],
                str(dump),
            ],
            config,
        )
        # Dump files deliberately omit this row. Missing control state denies
        # API writes and Worker claims, including a crash before this insert.
        with psycopg.connect(**config) as conn:
            conn.execute(
                "INSERT INTO system_control(id,maintenance,recovery_required,recovery_ledger_sha256) VALUES(1,true,true,NULL)"
            )
        for name, data in blobs.items():
            kind, _, relative = name.partition("/")
            if kind not in ("materials", "context"):
                raise ValueError("Unknown backup material type")
            root = settings.materials_root if kind == "materials" else settings.context_root
            root.mkdir(parents=True, exist_ok=True)
            destination = safe_file(root.resolve(), relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as material_file:
                material_file.write(data)
                material_file.flush()
                os.fsync(material_file.fileno())
        with psycopg.connect(**config) as conn:
            conn.execute("UPDATE system_control SET maintenance=true WHERE id=1")
            for entry in ledger:
                owner = entry.get("owner_id", entry.get("user_id"))
                memory_id = entry["memory_id"]
                excluded_sources = entry.get("source_ids", entry.get("source_event_ids", []))
                conn.execute(
                    "UPDATE memories SET data=(data - 'content' - 'facts' - 'conditions') || jsonb_build_object('content','','facts','{}'::jsonb,'status','forgotten'),version=version+1 WHERE owner_id=%s AND (id=%s OR (data->'source_event_ids') ?| %s)",
                    (owner, memory_id, excluded_sources),
                )
                for source_id in excluded_sources:
                    conn.execute(
                        "INSERT INTO source_states(owner_id,source_id,version,excluded) VALUES(%s,%s,1,true) ON CONFLICT(owner_id,source_id) DO UPDATE SET excluded=true,version=source_states.version+1",
                        (owner, source_id),
                    )
                conn.execute(
                    'UPDATE artifacts SET data=data || \'{"status":"deleted","preview":""}\'::jsonb WHERE owner_id=%s AND kind=\'context\'',
                    (owner,),
                )
                conn.execute(
                    "UPDATE task_work SET data=data - 'messages' - 'pending_decision' WHERE task_id IN (SELECT id FROM tasks WHERE owner_id=%s)",
                    (owner,),
                )
            conn.execute(
                "UPDATE tasks SET lease_epoch=lease_epoch+1,lease_owner=NULL,lease_until=NULL,input_version=input_version+1"
            )
            conn.execute(
                "UPDATE jobs SET status='queued',lease_owner=NULL,lease_until=NULL WHERE status='running'"
            )
            pending = scalar(
                conn.execute(
                    "SELECT count(*) FROM actions WHERE status IN ('confirmed','submitting','unknown')"
                )
            )
            # Simulation receipts can be reconciled without making any new business request.
            conn.execute(
                "UPDATE actions a SET status='accepted',receipt=t.data || jsonb_build_object('id',t.id) FROM tickets t WHERE t.operation_id=a.operation_id AND a.status IN ('confirmed','submitting','unknown')"
            )
            remaining = scalar(
                conn.execute(
                    "SELECT count(*) FROM actions WHERE status IN ('confirmed','submitting','unknown')"
                )
            )
            conn.execute(
                "UPDATE tasks t SET state='WAITING',wait_reason='human_service' WHERE EXISTS(SELECT 1 FROM actions a WHERE a.task_id=t.id AND a.status='accepted') AND t.state <> 'COMPLETED'"
            )
        if args.deletion_ledger.read_bytes() != ledger_bytes:
            raise ValueError(
                "Independent deletion ledger changed during restore; recovery remains locked"
            )
        ledger_target.parent.mkdir(parents=True, exist_ok=True)
        if ledger_target.resolve() != args.deletion_ledger.resolve():
            # Never truncate the independently retained source. Install the
            # separate runtime copy atomically so an interruption keeps it whole.
            with tempfile.NamedTemporaryFile(dir=ledger_target.parent, delete=False) as ledger_file:
                staged_ledger = Path(ledger_file.name)
                ledger_file.write(ledger_bytes)
                ledger_file.flush()
                os.fsync(ledger_file.fileno())
            try:
                os.replace(staged_ledger, ledger_target)
            finally:
                staged_ledger.unlink(missing_ok=True)
        ledger_hash = hashlib.sha256(ledger_bytes).hexdigest()
        if hashlib.sha256(ledger_target.read_bytes()).hexdigest() != ledger_hash:
            raise ValueError("Restored deletion ledger checksum mismatch")
        # Readiness is recorded only after every file and privacy change is
        # durable. Remaining operations still prevent the explicit reopen.
        with psycopg.connect(**config) as conn:
            conn.execute(
                "UPDATE system_control SET recovery_ledger_sha256=%s WHERE id=1 AND recovery_required",
                (ledger_hash,),
            )
        return {
            "restored": True,
            "maintenance": True,
            "recovery_required": True,
            "privacy_entries_applied": len(ledger),
            "pending_before": pending,
            "pending_remaining": remaining,
            "notice": "恢复后保持维护状态；核验文件、删除清单与未决动作后，由维护者显式开放。",
        }
