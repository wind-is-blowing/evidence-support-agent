import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from kefu.storage import schema as s


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(UTC)


def body_hash(body):
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class DomainError(Exception):
    def __init__(self, code, message, status=409, retryable=False):
        self.code, self.message, self.status, self.retryable = code, message, status, retryable
        super().__init__(message)


class Repository:
    def __init__(self, db):
        self.db = db

    async def task(self, conn, task_id, lock=False):
        query = sa.select(s.tasks).where(s.tasks.c.id == task_id)
        if lock:
            query = query.with_for_update()
        row = (await conn.execute(query)).mappings().first()
        if not row:
            raise DomainError("NOT_FOUND", "记录不存在或不可访问", 404)
        work = (
            await conn.execute(
                sa.select(s.task_work.c.data).where(s.task_work.c.task_id == task_id)
            )
        ).scalar_one()
        task = {**copy.deepcopy(work), **dict(row)}
        task["version"] = task["input_version"]
        return task

    async def emit(self, conn, task_id, actor_id, kind, payload, visibility="public"):
        seq = (
            await conn.execute(
                sa.update(s.tasks)
                .where(s.tasks.c.id == task_id)
                .values(last_event_seq=s.tasks.c.last_event_seq + 1)
                .returning(s.tasks.c.last_event_seq)
            )
        ).scalar_one()
        event = {
            "id": uid(),
            "task_id": task_id,
            "seq": seq,
            "actor_id": actor_id,
            "type": kind,
            "payload": {"schema_version": 1, **payload},
            "visibility": visibility,
        }
        await conn.execute(s.events.insert().values(**event))
        return event

    async def enqueue(self, conn, task_id, kind, version, target_id=None, delay=0):
        await conn.execute(
            insert(s.jobs)
            .values(
                id=uid(),
                task_id=task_id,
                kind=kind,
                input_version=version,
                target_id=target_id or task_id,
                available_at=sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, 0, delay),
            )
            .on_conflict_do_nothing(index_elements=["kind", "target_id", "input_version"])
        )

    async def save_work(self, conn, task):
        columns = set(s.tasks.c.keys())
        data = {k: v for k, v in task.items() if k not in columns and k != "version"}
        await conn.execute(
            s.task_work.update().where(s.task_work.c.task_id == task["id"]).values(data=data)
        )

    async def mutate_input(self, conn, task, state="READY", reason=None):
        task["input_version"] += 1
        task["version"] = task["input_version"]
        task["revision"] += 1
        task["state"], task["wait_reason"] = state, reason
        await conn.execute(
            s.tasks.update()
            .where(s.tasks.c.id == task["id"])
            .values(
                input_version=task["input_version"],
                revision=task["revision"],
                state=state,
                wait_reason=reason,
                updated_at=sa.func.now(),
                completed_at=sa.func.now() if state in ("COMPLETED", "CANCELLED") else None,
            )
        )
        await self.save_work(conn, task)
        await self.emit(
            conn,
            task["id"],
            task["owner_id"],
            "task_state",
            {"state": state, "wait_reason": reason, "input_version": task["input_version"]},
        )
        if state == "READY":
            await self.enqueue(conn, task["id"], "run_turn", task["input_version"])
        if state in ("COMPLETED", "CANCELLED"):
            await self.enqueue(conn, task["id"], "memory_extract", task["input_version"])
            await self.enqueue(conn, task["id"], "cleanup_artifacts", task["input_version"])

    async def repeated(self, conn, owner, task_id, key, body):
        row = (
            (
                await conn.execute(
                    sa.select(s.requests).where(
                        s.requests.c.principal_id == owner,
                        s.requests.c.task_id == task_id,
                        s.requests.c.client_event_id == key,
                    )
                )
            )
            .mappings()
            .first()
        )
        if row:
            if row["body_hash"] != body_hash(body):
                raise DomainError("IDEMPOTENCY_CONFLICT", "同一请求编号不能用于不同内容")
            return row["result"]
        return None

    async def remember_request(self, conn, owner, task_id, key, body, result):
        await conn.execute(
            s.requests.insert().values(
                principal_id=owner,
                task_id=task_id,
                client_event_id=key,
                body_hash=body_hash(body),
                result=result,
            )
        )

    async def snapshot(self, owner, task_id, staff=False):
        from kefu.storage.sources import invalid_source_keys, sanitize_messages, source_key

        async with self.db.transaction(owner) as conn:
            task = await self.task(conn, task_id)
            evidence = task.get("evidence", [])
            invalid = await invalid_source_keys(conn, evidence)
            blocked_refs = {entry.get("id") for entry in evidence if source_key(entry) in invalid}
            task["evidence"] = [entry for entry in evidence if entry.get("id") not in blocked_refs]
            if blocked_refs.intersection((task.get("current_card") or {}).get("evidence_refs", [])):
                task["current_card"] = None
            rows = (
                (
                    await conn.execute(
                        sa.select(s.events)
                        .where(s.events.c.task_id == task_id)
                        .order_by(s.events.c.seq)
                    )
                )
                .mappings()
                .all()
            )
            # Consumer projection never replays raw tool/model context or internal notes.
            rows = await sanitize_messages(conn, rows)
            task["messages"] = [
                {"id": e["id"], "created_at": e["created_at"].isoformat(), **e["payload"]}
                for e in rows
                if e["type"] == "message"
                and (staff or e["visibility"] == "public")
                and not e["payload"].get("retention_deleted")
                and e["payload"].get("retained") is not False
            ]
            if blocked_refs:
                for message in task["messages"]:
                    if message.get("role") == "assistant" and (
                        not message.get("evidence_refs")
                        or blocked_refs.intersection(message["evidence_refs"])
                    ):
                        message["text"] = "这条回复所依据的资料已撤回，请重新核对当前资料。"
                        message["evidence_refs"] = []
            task["receipts"] = [
                (dict(row)["data"] | {"id": row["id"]})
                for row in (
                    await conn.execute(sa.select(s.tickets).where(s.tickets.c.task_id == task_id))
                ).mappings()
            ]
            task["materials"] = [
                (row["data"] | {"id": row["id"], "url": "/api/materials/" + row["id"]})
                for row in (
                    await conn.execute(
                        sa.select(s.artifacts).where(
                            s.artifacts.c.task_id == task_id, s.artifacts.c.kind == "upload"
                        )
                    )
                ).mappings()
                if row["data"].get("status") not in ("deleted", "tombstoned")
            ]
            task["is_simulated"] = self.db.settings.simulated_business
            task["model_mode"] = self.db.settings.model_mode
            task["can_resume"] = task["state"] in ("PAUSED", "CANCELLED") and task[
                "wait_reason"
            ] not in ("budget_exhausted", "action_unknown")
            task["pause_reason"] = task.get("wait_reason")
            allowed = {
                "id",
                "title",
                "state",
                "wait_reason",
                "pause_reason",
                "revision",
                "input_version",
                "version",
                "product_version",
                "last_event_seq",
                "messages",
                "product",
                "current_card",
                "steps",
                "receipts",
                "materials",
                "is_simulated",
                "model_mode",
                "can_resume",
                "created_at",
                "updated_at",
                "region",
                "channel",
                "todo",
                "device_id",
                "purchase",
                "warranty",
                "evidence",
                "urgency",
                "service_signals",
                "service_intent",
                "image_observations",
                "purchase_lookup",
            }
            if staff:
                allowed |= {"goal", "draft", "assigned_staff_id"}
            return {k: v for k, v in task.items() if k in allowed}

    async def reserve_budget(self, owner, task_id, call_id, tokens, reservation=None):
        if tokens <= 0:
            return False
        async with self.db.transaction(owner) as conn:
            day = (
                await conn.execute(sa.text("SELECT to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD')"))
            ).scalar_one()
            await conn.execute(
                insert(s.budget_buckets)
                .values(day=day, limit=self.db.settings.daily_token_limit)
                .on_conflict_do_nothing()
            )
            bucket = (
                (
                    await conn.execute(
                        sa.select(s.budget_buckets)
                        .where(s.budget_buckets.c.day == day)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            task = await self.task(conn, task_id, lock=True)
            if reservation is not None:
                active = (
                    await conn.execute(
                        sa.select(s.tasks.c.id).where(
                            s.tasks.c.id == task_id,
                            s.tasks.c.input_version == reservation.get("input_version"),
                            s.tasks.c.lease_epoch == reservation.get("lease_epoch"),
                            s.tasks.c.lease_owner == reservation.get("lease_owner"),
                            s.tasks.c.lease_until > sa.func.now(),
                            s.tasks.c.state.in_(["READY", "RUNNING"]),
                        )
                    )
                ).first()
                if not active or task.get("stop_requested"):
                    return False
            if (
                await conn.execute(
                    sa.select(s.model_calls.c.id).where(s.model_calls.c.id == call_id)
                )
            ).first():
                return False
            if (
                bucket["used"] + bucket["reserved"] + tokens > bucket["limit"]
                or task["tokens_used"] + task["tokens_reserved"] + tokens > task["token_limit"]
            ):
                return False
            await conn.execute(
                s.budget_buckets.update()
                .where(s.budget_buckets.c.day == day)
                .values(reserved=s.budget_buckets.c.reserved + tokens)
            )
            await conn.execute(
                s.tasks.update()
                .where(s.tasks.c.id == task_id)
                .values(tokens_reserved=s.tasks.c.tokens_reserved + tokens)
            )
            await conn.execute(
                s.model_calls.insert().values(
                    id=call_id,
                    task_id=task_id,
                    day=day,
                    reserved=tokens,
                    status="reserved",
                    data=reservation or {},
                )
            )
            return True

    async def settle_budget(self, owner, call_id, usage, recovery_lease=None):
        async with self.db.transaction(owner) as conn:
            call = (
                (await conn.execute(sa.select(s.model_calls).where(s.model_calls.c.id == call_id)))
                .mappings()
                .one()
            )
            await conn.execute(
                sa.select(s.budget_buckets)
                .where(s.budget_buckets.c.day == call["day"])
                .with_for_update()
            )
            await conn.execute(
                sa.select(s.tasks).where(s.tasks.c.id == call["task_id"]).with_for_update()
            )
            if recovery_lease is not None:
                active = (
                    await conn.execute(
                        sa.select(s.tasks.c.id).where(
                            s.tasks.c.id == call["task_id"],
                            s.tasks.c.lease_epoch == recovery_lease["lease_epoch"],
                            s.tasks.c.lease_owner == recovery_lease["lease_owner"],
                            s.tasks.c.lease_until > sa.func.now(),
                        )
                    )
                ).first()
                if not active:
                    return False
            call = (
                (
                    await conn.execute(
                        sa.select(s.model_calls)
                        .where(s.model_calls.c.id == call_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if call["status"] != "reserved":
                return False
            if (
                recovery_lease is not None
                and call["data"].get("lease_epoch") == recovery_lease["lease_epoch"]
            ):
                # A live request from the new lease must settle itself.
                return False
            actual = usage.get("total_tokens")
            unknown = not isinstance(actual, int) or isinstance(actual, bool) or actual < 0
            used = call["reserved"] if unknown else actual
            await conn.execute(
                s.budget_buckets.update()
                .where(s.budget_buckets.c.day == call["day"])
                .values(
                    reserved=s.budget_buckets.c.reserved - call["reserved"],
                    used=s.budget_buckets.c.used + used,
                )
            )
            await conn.execute(
                s.tasks.update()
                .where(s.tasks.c.id == call["task_id"])
                .values(
                    tokens_reserved=s.tasks.c.tokens_reserved - call["reserved"],
                    tokens_used=s.tasks.c.tokens_used + used,
                )
            )
            await conn.execute(
                s.model_calls.update()
                .where(s.model_calls.c.id == call_id)
                .values(
                    used=used,
                    status="estimated" if unknown else "settled",
                    data={**call["data"], **usage},
                )
            )
            return True

    async def recover_model_reservations(self, owner, task_id, lease_owner, lease_epoch):
        """Charge abandoned reservations once under a freshly acquired task lease.

        Unknown legacy calls are included: the new lease excludes every former
        executor. A late old response cannot refund or double-charge this entry.
        Each settlement retains the daily-bucket -> task -> call lock order.
        """
        async with self.db.transaction(owner) as conn:
            call_ids = list(
                (
                    await conn.execute(
                        sa.select(s.model_calls.c.id).where(
                            s.model_calls.c.task_id == task_id,
                            s.model_calls.c.status == "reserved",
                        )
                    )
                ).scalars()
            )
        recovered = 0
        for call_id in call_ids:
            recovered += bool(
                await self.settle_budget(
                    owner,
                    call_id,
                    {"status": "unknown", "reason": "orphaned_lease_reservation"},
                    recovery_lease={"lease_owner": lease_owner, "lease_epoch": lease_epoch},
                )
            )
        return recovered
