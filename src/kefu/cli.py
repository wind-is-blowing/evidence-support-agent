"""Explicit maintenance commands. No arbitrary SQL or arbitrary HTTP interface."""

import argparse
import asyncio
import getpass
import hashlib
import json
import secrets
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from kefu import configure_asyncio
from kefu.api.auth import password_hasher
from kefu.plugins.knowledge import KnowledgePlugin, configured_embedding
from kefu.settings import Settings
from kefu.storage import schema as s
from kefu.storage.database import Database
from kefu.storage.plugins import PluginRepository
from kefu.storage.repository import Repository, uid

DEMO_PRODUCTS = [
    ("soundcore-a3947", "Liberty 4 NC", "A3947", "2026-08-18T10:30:00+08:00", 699),
    ("eufy-t2080", "S1 Pro", "T2080", "2026-06-09T15:20:00+08:00", 9999),
    ("anker-a2148", "737 Charger (GaNPrime 120W)", "A2148", "2026-09-03T19:05:00+08:00", 599),
    ("soundcore-a3035", "Space One", "A3035", "2026-07-22T12:10:00+08:00", 699),
    ("eufy-t2351", "X10 Pro Omni", "T2351", "2026-05-16T09:40:00+08:00", 5999),
    ("anker-a1336", "Prime 20,000mAh Power Bank (200W)", "A1336", "2026-08-30T16:50:00+08:00", 999),
]


async def seed_demo_store(db, import_url: str, consumer_id: str) -> dict[str, int]:
    product_ids = [item[0] for item in DEMO_PRODUCTS]
    dealer_id = "anker-direct-demo-cn"
    inserted = 0
    import_engine = create_async_engine(import_url)
    try:
        async with import_engine.begin() as conn:
            dealer = (
                (await conn.execute(sa.select(s.dealers).where(s.dealers.c.id == dealer_id)))
                .mappings()
                .first()
            )
            if not dealer:
                await conn.execute(
                    s.dealers.insert().values(
                        id=dealer_id,
                        data={
                            "name": "安克创新官方直营综合店（赛事模拟）",
                            "verified": True,
                            "authorized": True,
                            "regions": ["CN"],
                            "product_ids": product_ids,
                            "is_simulated": True,
                        },
                    )
                )
            elif not dealer["data"].get("is_simulated"):
                raise ValueError("Refusing to replace a non-simulated dealer")
            partner_id = "contest-partner-demo-cn"
            if not (
                await conn.execute(sa.select(s.dealers.c.id).where(s.dealers.c.id == partner_id))
            ).first():
                await conn.execute(
                    s.dealers.insert().values(
                        id=partner_id,
                        data={
                            "name": "安心数码（赛事模拟经销商）",
                            "aliases": ["安心数码"],
                            "verified": True,
                            "authorized": True,
                            "regions": ["CN"],
                            "product_ids": ["eufy-t2080"],
                            "is_simulated": True,
                        },
                    )
                )
    finally:
        await import_engine.dispose()
    async with db.transaction(consumer_id) as conn:
        for index, (product_id, name, model, purchased_at, amount) in enumerate(DEMO_PRODUCTS, 1):
            purchase_id = f"demo-order-{index:04d}"
            exists = (
                await conn.execute(
                    sa.select(s.purchases.c.owner_id).where(s.purchases.c.id == purchase_id)
                )
            ).scalar_one_or_none()
            if exists:
                if exists != consumer_id:
                    raise ValueError("A simulated order ID belongs to another consumer")
                continue
            await conn.execute(
                s.purchases.insert().values(
                    id=purchase_id,
                    owner_id=consumer_id,
                    product_id=product_id,
                    data={
                        "order_number": f"DEMO-2026-{index:04d}",
                        "product_name": name,
                        "model": model,
                        "purchased_at": purchased_at,
                        "status": "delivered",
                        "channel": "anker_direct_demo",
                        "region": "CN",
                        "dealer_id": dealer_id,
                        "quantity": 1,
                        "amount": {"currency": "CNY", "value": amount},
                        "verified": True,
                        "is_simulated": True,
                    },
                )
            )
            inserted += 1
    return {"orders_inserted": inserted, "orders_total": len(DEMO_PRODUCTS), "dealers": 2}


async def set_maintenance(conn, enabled, ledger_path):
    """Ordinary pauses preserve queued work; restored data has a separate gate."""
    from kefu.storage.backup import MAINTENANCE_LOCK

    await conn.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": MAINTENANCE_LOCK})
    control = (
        (await conn.execute(sa.text("SELECT * FROM system_control WHERE id=1 FOR UPDATE")))
        .mappings()
        .one_or_none()
    )
    if control is None:
        raise ValueError("System control is missing; complete the isolated restore first")
    if not enabled and control["recovery_required"]:
        expected_hash = control["recovery_ledger_sha256"]
        if not expected_hash:
            raise ValueError("Restore validation is incomplete; recovery remains locked")
        if not ledger_path.is_file() or ledger_path.is_symlink():
            raise ValueError(
                "Independent deletion ledger must be available before service is opened"
            )
        if hashlib.sha256(ledger_path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError(
                "Deletion ledger changed after validation; reapply it before reopening"
            )
        pending = (
            await conn.execute(
                sa.text(
                    "SELECT count(*) FROM actions WHERE status IN ('confirmed','submitting','unknown')"
                )
            )
        ).scalar_one()
        if pending:
            raise ValueError(
                "Unresolved business operations must be reconciled before opening restored service"
            )
        await conn.execute(
            sa.text(
                "UPDATE system_control SET recovery_required=false,recovery_ledger_sha256=NULL WHERE id=1"
            )
        )
    await conn.execute(
        sa.text("UPDATE system_control SET maintenance=:enabled WHERE id=1"), {"enabled": enabled}
    )


async def create_user(db, username, role, display_name, password):
    encoded = await asyncio.to_thread(password_hasher.hash, password)
    principal_id = uid()
    async with db.transaction() as conn:
        await conn.execute(
            s.principals.insert().values(
                id=principal_id,
                username=username,
                role=role,
                display_name=display_name or username,
                password_hash=encoded,
            )
        )
    return principal_id


async def seed_demo(db):
    settings = db.settings
    if (
        settings.environment not in ("local", "test")
        or not settings.simulated_business
        or settings.model_mode != "demo"
    ):
        raise ValueError("seed-demo only runs in a dedicated local/test simulated environment")
    if not settings.import_database_url:
        raise ValueError("IMPORT_DATABASE_URL is required for seed knowledge")
    plugins = PluginRepository(db, settings.import_database_url.get_secret_value())
    try:
        knowledge = KnowledgePlugin(
            plugins, embedder=configured_embedding(settings), allow_synthetic=True
        )
        imported = await knowledge.import_manifest("data/sources.jsonl")
        await knowledge.import_manifest("data/contest.jsonl")
        await knowledge.import_manifest("data/official_expansion_2026-10-08.jsonl")
        await knowledge.import_manifest("data/official_products_2026-10-08.jsonl")
        await knowledge.import_manifest("data/response_manual.jsonl")
        await knowledge.import_manifest("data/response_manual_expansion_2026-10-08.jsonl")
        await knowledge.import_manifest("data/demo.jsonl")
        await knowledge.import_manifest("data/store_demo.jsonl")
    finally:
        await plugins.close()
    users = []
    account_ids = {}
    for username, role in (("demo-consumer", "consumer"), ("demo-staff", "staff")):
        async with db.transaction() as conn:
            exists = (
                await conn.execute(
                    sa.select(s.principals.c.id).where(s.principals.c.username == username)
                )
            ).scalar_one_or_none()
        if exists:
            account_ids[username] = exists
            continue
        password = secrets.token_urlsafe(18)
        principal_id = await create_user(db, username, role, username, password)
        account_ids[username] = principal_id
        users.append({"username": username, "password": password, "id": principal_id})
    store = await seed_demo_store(
        db, settings.import_database_url.get_secret_value(), account_ids["demo-consumer"]
    )
    return {
        "is_simulated": True,
        "knowledge": imported,
        "store": store,
        "created_accounts": users,
        "notice": "随机密码仅显示一次；已存在账号保持原密码。门店、订单、政策和 Demo Cleaner 均为赛事模拟数据。",
    }


async def run(args):
    if args.command == "collect-official":
        from kefu.plugins.official_collection import collect_official_sources

        collection_result = await collect_official_sources(args.config, args.output)
        print(json.dumps(collection_result, ensure_ascii=False, indent=2))
        return
    settings = Settings()
    db = Database(settings)
    try:
        if args.command == "create-user":
            password = getpass.getpass("Password (minimum 6 characters): ")
            if len(password) < 6 or password != getpass.getpass("Confirm password: "):
                raise ValueError("Passwords must match and contain at least 6 characters")
            print(
                json.dumps(
                    {
                        "id": await create_user(
                            db, args.username, args.role, args.display_name, password
                        )
                    }
                )
            )
        elif args.command == "seed-demo":
            print(json.dumps(await seed_demo(db), ensure_ascii=False, indent=2))
        elif args.command == "import-knowledge":
            if not settings.import_database_url:
                raise ValueError("IMPORT_DATABASE_URL is required")
            plugin_repo = PluginRepository(db, settings.import_database_url.get_secret_value())
            try:
                import_result = await KnowledgePlugin(
                    plugin_repo,
                    embedder=configured_embedding(settings),
                    allow_synthetic=settings.environment != "production",
                ).import_manifest(args.manifest)
                print(json.dumps(import_result))
            finally:
                await plugin_repo.close()
        elif args.command == "prepare-knowledge":
            from kefu.plugins.ingestion import prepare_knowledge

            preparation_result = await asyncio.to_thread(
                prepare_knowledge, args.source, args.metadata, args.output
            )
            print(json.dumps(preparation_result, ensure_ascii=False, default=str, indent=2))
        elif args.command == "cleanup-files":
            from kefu.storage.files import FileStore

            if not settings.migration_database_url:
                raise ValueError("MIGRATION_DATABASE_URL is required in this maintenance process")
            engine = create_async_engine(settings.migration_database_url.get_secret_value())
            removed = 0
            try:
                async with engine.begin() as conn:
                    rows = (
                        (
                            await conn.execute(
                                sa.select(s.artifacts)
                                .where(
                                    s.artifacts.c.kind.in_(["upload", "deleted_upload"]),
                                    s.artifacts.c.data["status"].astext.in_(
                                        ["deleted", "tombstoned"]
                                    ),
                                )
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .all()
                    )
                    files = FileStore(settings.materials_root)
                    for row in rows:
                        if row["data"].get("physically_deleted"):
                            continue
                        await asyncio.to_thread(files.unlink, row["data"]["storage_key"])
                        await conn.execute(
                            s.artifacts.update()
                            .where(s.artifacts.c.id == row["id"])
                            .values(
                                data={
                                    **row["data"],
                                    "physically_deleted": True,
                                    "purge_pending": False,
                                }
                            )
                        )
                        removed += 1
                print(json.dumps({"removed_files": removed}))
            finally:
                await engine.dispose()
        elif args.command == "maintenance":
            if not settings.migration_database_url:
                raise ValueError("MIGRATION_DATABASE_URL is required")
            engine = create_async_engine(settings.migration_database_url.get_secret_value())
            try:
                async with engine.begin() as conn:
                    await set_maintenance(conn, args.enabled, settings.deletion_ledger_path)
                    print(json.dumps({"maintenance": args.enabled}))
            finally:
                await engine.dispose()
        elif args.command in ("assign-task", "inspect-task", "set-budget"):
            if not settings.migration_database_url:
                raise ValueError(
                    "Maintenance commands require MIGRATION_DATABASE_URL, never install it in API/Worker"
                )
            engine = create_async_engine(settings.migration_database_url.get_secret_value())
            try:
                async with engine.begin() as conn:
                    task = (
                        (
                            await conn.execute(
                                sa.select(s.tasks)
                                .where(s.tasks.c.id == args.task_id)
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if args.command == "assign-task":
                        staff = (
                            (
                                await conn.execute(
                                    sa.select(s.principals).where(
                                        s.principals.c.username == args.staff,
                                        s.principals.c.role == "staff",
                                    )
                                )
                            )
                            .mappings()
                            .one()
                        )
                        await conn.execute(
                            s.tasks.update()
                            .where(s.tasks.c.id == args.task_id)
                            .values(assigned_staff_id=staff["id"], revision=s.tasks.c.revision + 1)
                        )
                        print(
                            json.dumps({"task_id": args.task_id, "assigned_staff_id": staff["id"]})
                        )
                    elif args.command == "set-budget":
                        if args.tokens <= 0:
                            raise ValueError("budget must be positive")
                        await conn.execute(
                            s.tasks.update()
                            .where(s.tasks.c.id == args.task_id)
                            .values(token_limit=args.tokens, revision=s.tasks.c.revision + 1)
                        )
                        await Repository(db).emit(
                            conn,
                            args.task_id,
                            "maintainer",
                            "budget_adjusted",
                            {"old": task["token_limit"], "new": args.tokens},
                            "internal",
                        )
                        print(json.dumps({"task_id": args.task_id, "token_limit": args.tokens}))
                    else:
                        counts = {}
                        for name, table in (
                            ("jobs", s.jobs),
                            ("actions", s.actions),
                            ("model_calls", s.model_calls),
                        ):
                            counts[name] = list(
                                (
                                    await conn.execute(
                                        sa.select(table).where(table.c.task_id == args.task_id)
                                    )
                                ).mappings()
                            )
                        # Diagnostics omit messages, drafts, credentials and original materials.
                        safe = {
                            k: task[k]
                            for k in (
                                "id",
                                "state",
                                "wait_reason",
                                "revision",
                                "input_version",
                                "lease_epoch",
                                "token_limit",
                                "tokens_used",
                                "tokens_reserved",
                            )
                        }
                        safe["jobs"] = [
                            {k: r[k] for k in ("id", "kind", "status", "attempts", "available_at")}
                            for r in counts["jobs"]
                        ]
                        safe["actions"] = [
                            {k: r[k] for k in ("id", "operation_id", "status", "attempts")}
                            for r in counts["actions"]
                        ]
                        safe["model_calls"] = [
                            {k: r[k] for k in ("id", "status", "used", "reserved")}
                            for r in counts["model_calls"]
                        ]
                        print(json.dumps(safe, ensure_ascii=False, default=str, indent=2))
            finally:
                await engine.dispose()
        elif args.command in ("backup", "restore"):
            from kefu.storage.backup import backup, restore

            function = backup if args.command == "backup" else restore
            print(
                json.dumps(
                    await asyncio.to_thread(function, settings, args),
                    ensure_ascii=False,
                    default=str,
                    indent=2,
                )
            )
    finally:
        await db.close()


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    user = sub.add_parser("create-user")
    user.add_argument("username")
    user.add_argument("--role", choices=["consumer", "staff"], default="consumer")
    user.add_argument("--display-name")
    sub.add_parser("seed-demo")
    sub.add_parser("cleanup-files")
    importer = sub.add_parser("import-knowledge")
    importer.add_argument("--manifest", required=True, type=Path)
    preparer = sub.add_parser("prepare-knowledge")
    preparer.add_argument("--source", type=Path, required=True)
    preparer.add_argument("--metadata", type=Path, required=True)
    preparer.add_argument("--output", type=Path, required=True)
    collector = sub.add_parser("collect-official")
    collector.add_argument("--config", type=Path, required=True)
    collector.add_argument("--output", type=Path, required=True)
    maintenance = sub.add_parser("maintenance")
    maintenance.add_argument("--enabled", action=argparse.BooleanOptionalAction, required=True)
    assign = sub.add_parser("assign-task")
    assign.add_argument("task_id")
    assign.add_argument("--staff", required=True)
    inspect = sub.add_parser("inspect-task")
    inspect.add_argument("task_id")
    budget = sub.add_parser("set-budget")
    budget.add_argument("task_id")
    budget.add_argument("--tokens", type=int, required=True)
    for name in ("backup", "restore"):
        command = sub.add_parser(name)
        command.add_argument("--path", type=Path, required=True)
        command.add_argument("--key-file", type=Path, required=True)
        if name == "restore":
            command.add_argument("--deletion-ledger", type=Path, required=True)
    return root


def main():
    configure_asyncio()
    try:
        asyncio.run(run(parser().parse_args()))
    except (ValueError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
