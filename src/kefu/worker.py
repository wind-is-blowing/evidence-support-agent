import asyncio
import contextlib
import logging
import signal

import sqlalchemy as sa
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.dialects.postgresql import insert

from kefu import configure_asyncio
from kefu.plugins.model import DemoModel, QwenModel
from kefu.plugins.tools import ToolExecutor
from kefu.runtime.loop import Runtime
from kefu.services import ToolServices
from kefu.settings import Settings
from kefu.storage import schema as s
from kefu.storage.database import Database
from kefu.storage.repository import DomainError, Repository, uid

log = logging.getLogger("kefu.worker")


class WorkerPorts:
    def __init__(self, worker, job):
        self.worker, self.job = worker, job
        self.owner, self.epoch = job["owner_id"], job["lease_epoch"]

    async def is_current(self, task_id, version):
        async with self.worker.db.transaction(self.owner) as conn:
            return bool(
                (
                    await conn.execute(
                        sa.select(s.tasks.c.id).where(
                            s.tasks.c.id == task_id,
                            s.tasks.c.input_version == version,
                            s.tasks.c.lease_epoch == self.epoch,
                            s.tasks.c.lease_owner == self.worker.worker_id,
                            s.tasks.c.lease_until > sa.func.now(),
                        )
                    )
                ).first()
            )

    async def checkpoint(self, task, event):
        worker, task_id = self.worker, task["id"]
        invalid_source = False
        async with worker.db.transaction(self.owner) as conn:
            current = await worker.repo.task(conn, task_id, True)
            active = (
                await conn.execute(
                    sa.select(s.tasks.c.id).where(
                        s.tasks.c.id == task_id,
                        s.tasks.c.input_version == task["version"],
                        s.tasks.c.lease_epoch == self.epoch,
                        s.tasks.c.lease_owner == worker.worker_id,
                        s.tasks.c.lease_until > sa.func.now(),
                    )
                )
            ).first()
            if not active:
                raise DomainError("STALE_LEASE", "执行租约或输入已失效")
            publishing = event["type"] in {"model_decision", "assistant_message"} or (
                event["type"] == "tool_result" and task.get("current_card")
            )
            if publishing and not await self._sources_current(conn, task):
                invalid_source = True
                await self._invalidate_sources(conn, task)
                # Persist the invalidation before raising, without publishing
                # any buffered response or overwriting newly authenticated facts.
                task = current
                for key in (
                    "evidence",
                    "evidence_refs",
                    "retrieved_steps",
                    "pending_decision",
                    "current_card",
                    "current_step",
                    "messages",
                ):
                    task.pop(key, None)
                task.update(state="PAUSED", waiting_reason="knowledge_changed")
                event = {"type": "turn_finished"}
            await worker.repo.save_work(conn, task)
            await conn.execute(
                s.tasks.update()
                .where(s.tasks.c.id == task_id)
                .values(
                    state=task["state"],
                    wait_reason=task.get("waiting_reason"),
                    revision=s.tasks.c.revision + 1,
                    updated_at=sa.func.now(),
                )
            )
            kind = event["type"]
            if kind == "assistant_message":
                await worker.repo.emit(
                    conn,
                    task_id,
                    self.owner,
                    "message",
                    {
                        "role": "assistant",
                        "text": event["content"],
                        "evidence_refs": task.get("evidence_refs", []),
                        "source_versions": [
                            {key: entry[key] for key in ("document_id", "version", "chunk_id")}
                            for entry in task.get("evidence", [])
                        ],
                    },
                )
            elif kind == "tool_result" and (task.get("current_card") or {}).get("type"):
                card = task["current_card"]
                await worker.repo.emit(
                    conn,
                    task_id,
                    self.owner,
                    {
                        "product": "product_card",
                        "step": "step_card",
                        "escalation": "escalation_card",
                    }[card["type"]],
                    card,
                )
            elif kind == "turn_started":
                await worker.repo.emit(
                    conn, task_id, self.owner, "progress", {"message": "正在核对产品资料"}
                )
            elif kind == "turn_finished":
                await worker.repo.emit(
                    conn,
                    task_id,
                    self.owner,
                    "task_state",
                    {"state": task["state"], "wait_reason": task.get("waiting_reason")},
                )
            else:
                # Internal protocol trace deliberately excludes prompts, images and model reasoning.
                safe = {k: v for k, v in event.items() if k not in ("content", "result")}
                await worker.repo.emit(conn, task_id, self.owner, "runtime", safe, "internal")
        if invalid_source:
            raise DomainError("KNOWLEDGE_CHANGED", "知识来源已撤回或改变，请重新检索后继续")

    async def _invalidate_sources(self, conn, task):
        source_keys = {
            (entry.get("document_id"), entry.get("version"), entry.get("chunk_id"))
            for entry in task.get("evidence", [])
        }
        messages = (
            await conn.execute(
                sa.select(s.events).where(
                    s.events.c.task_id == task["id"],
                    s.events.c.type == "message",
                    s.events.c.payload["role"].astext == "assistant",
                )
            )
        ).mappings()
        for message in messages:
            payload = message["payload"]
            versions = payload.get("source_versions", [])
            if not versions or source_keys.intersection(
                (entry.get("document_id"), entry.get("version"), entry.get("chunk_id"))
                for entry in versions
            ):
                await conn.execute(
                    s.events.update()
                    .where(s.events.c.id == message["id"])
                    .values(payload={**payload, "source_invalidated": True})
                )
        artifacts = (
            await conn.execute(
                sa.select(s.artifacts).where(
                    s.artifacts.c.task_id == task["id"],
                    s.artifacts.c.owner_id == self.owner,
                    s.artifacts.c.kind == "context",
                )
            )
        ).mappings()
        for artifact in artifacts:
            await conn.execute(
                s.artifacts.update()
                .where(s.artifacts.c.id == artifact["id"])
                .values(
                    data={
                        **artifact["data"],
                        "status": "deleted",
                        "preview": "",
                        "source_invalidated": True,
                    }
                )
            )

    async def _sources_current(self, conn, task):
        from kefu.storage.sources import invalid_source_keys

        return not await invalid_source_keys(conn, task.get("evidence", []))

    async def reserve_budget(self, task_id, call_id, tokens):
        return await self.worker.repo.reserve_budget(
            self.owner,
            task_id,
            call_id,
            tokens,
            reservation={
                "lease_epoch": self.epoch,
                "lease_owner": self.worker.worker_id,
                "input_version": self.job["input_version"],
            },
        )

    async def settle_budget(self, call_id, usage):
        await self.worker.repo.settle_budget(self.owner, call_id, usage)

    async def call_tool(self, name, arguments, task):
        return await self.worker.services.call(name, arguments, task, self)

    async def compact_tool_result(self, call_id, name, output, task):
        return await self.worker.services.compact_tool_result(call_id, name, output, task)

    async def build_context(self, task, events):
        return await self.worker.services.build_context(task, events)


class Worker:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        self.db = Database(self.settings)
        self.repo = Repository(self.db)
        self.worker_id = uid()
        self.model = (
            DemoModel()
            if self.settings.model_mode == "demo"
            else QwenModel(
                api_key=self.settings.model_api_key.get_secret_value(),
                base_url=self.settings.model_base_url,
                model=self.settings.model_name,
                provider=self.settings.model_provider,
            )
        )
        self.services = ToolServices(self.db, self.model)
        self.runtime = Runtime(self.model, executor=ToolExecutor(self.settings.skills_root))
        self.stopping = asyncio.Event()
        self.wakeup = asyncio.Event()

    async def claim(self):
        if self.settings.maintenance:
            return None
        async with self.db.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        sa.text("SELECT * FROM kefu_claim_job(:worker,:duration)"),
                        {"worker": self.worker_id, "duration": self.settings.lease_seconds},
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(min(15, self.settings.lease_seconds / 3))
            async with self.db.transaction(job["owner_id"]) as conn:
                updated = (
                    await conn.execute(
                        s.tasks.update()
                        .where(
                            s.tasks.c.id == job["task_id"],
                            s.tasks.c.lease_epoch == job["lease_epoch"],
                            s.tasks.c.lease_owner == self.worker_id,
                            s.tasks.c.lease_until > sa.func.now(),
                        )
                        .values(
                            lease_until=sa.func.now()
                            + sa.func.make_interval(0, 0, 0, 0, 0, 0, self.settings.lease_seconds)
                        )
                        .returning(s.tasks.c.id)
                    )
                ).first()
                if not updated:
                    return
                await conn.execute(
                    s.jobs.update()
                    .where(s.jobs.c.id == job["job_id"], s.jobs.c.lease_owner == self.worker_id)
                    .values(
                        lease_until=sa.func.now()
                        + sa.func.make_interval(0, 0, 0, 0, 0, 0, self.settings.lease_seconds)
                    )
                )

    async def process_one(self):
        job = await self.claim()
        if not job:
            return False
        pulse = asyncio.create_task(self.heartbeat(job))
        try:
            await self.repo.recover_model_reservations(
                job["owner_id"], job["task_id"], self.worker_id, job["lease_epoch"]
            )
            ports = WorkerPorts(self, job)
            async with self.db.transaction(job["owner_id"]) as conn:
                task = await self.repo.task(conn, job["task_id"])
                role = (
                    await conn.execute(
                        sa.select(s.principals.c.role).where(s.principals.c.id == job["owner_id"])
                    )
                ).scalar_one()
                task["principal_role"] = role
                events = [
                    dict(row)
                    for row in (
                        await conn.execute(
                            sa.select(s.events)
                            .where(
                                s.events.c.task_id == task["id"],
                                s.events.c.visibility == "public",
                                s.events.c.type == "message",
                            )
                            .order_by(s.events.c.seq)
                        )
                    ).mappings()
                ]
                for event in events:
                    event["created_at"] = event["created_at"].isoformat()
            if job["kind"] == "run_turn":
                if task["input_version"] == job["input_version"]:
                    async with self.db.transaction(job["owner_id"]) as conn:
                        pending = (
                            await conn.execute(
                                sa.select(s.actions.c.id).where(
                                    s.actions.c.task_id == task["id"],
                                    s.actions.c.status.in_(["confirmed", "submitting", "unknown"]),
                                )
                            )
                        ).first()
                        if pending and await self._owns_lease(conn, job):
                            # Preserve this input's durable job until the side
                            # effect is reconciled. Do not consume it as "done".
                            await conn.execute(
                                s.jobs.update()
                                .where(s.jobs.c.id == job["job_id"])
                                .values(status="blocked")
                            )
                            await conn.execute(
                                s.tasks.update()
                                .where(s.tasks.c.id == task["id"])
                                .values(state="WAITING", wait_reason="action_reconciliation")
                            )
                    if not pending:
                        task["pending_action"] = False
                        await self.runtime.run_turn(task, events, ports)
            elif job["kind"] == "reconcile_action":
                await self.reconcile(job)
            elif job["kind"] == "memory_extract":
                await self.extract_memory(task, events)
            elif job["kind"] == "reconcile_budget":
                if task["state"] in ("COMPLETED", "CANCELLED"):
                    async with self.db.transaction(job["owner_id"]) as conn:
                        if not await self._owns_lease(conn, job):
                            raise DomainError("STALE_LEASE", "旧执行者不得安排预算回收")
                        await self.repo.enqueue(
                            conn,
                            task["id"],
                            "cleanup_artifacts",
                            task["input_version"],
                            task["id"] + ":budget-cleanup:" + str(job["lease_epoch"]),
                        )
            elif job["kind"] == "cleanup_artifacts":
                async with self.db.transaction(job["owner_id"]) as conn:
                    pending = (
                        await conn.execute(
                            sa.select(s.actions.c.id).where(
                                s.actions.c.task_id == task["id"],
                                s.actions.c.status.in_(["confirmed", "submitting", "unknown"]),
                            )
                        )
                    ).first()
                if task["state"] in ("COMPLETED", "CANCELLED") and not pending:
                    if job["target_id"] == task["id"] + ":retention":
                        await self.retire_task_content(job)
                    else:
                        await self.services.context.cleanup(
                            task["id"],
                            job["owner_id"],
                            task["state"],
                            running_calls=False,
                            unresolved_operations=False,
                        )
                        async with self.db.transaction(job["owner_id"]) as conn:
                            await self.repo.enqueue(
                                conn,
                                task["id"],
                                "cleanup_artifacts",
                                task["input_version"],
                                task["id"] + ":retention",
                                delay=30 * 86400,
                            )
            await self.finish_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "job_failed task_id=%s kind=%s error=%s code=%s",
                job["task_id"],
                job["kind"],
                type(exc).__name__,
                exc.code if isinstance(exc, DomainError) else "",
            )
            await self.finish_job(job, failed=True)
        finally:
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pulse
        return True

    async def finish_job(self, job, failed=False):
        async with self.db.transaction(job["owner_id"]) as conn:
            task = await self.repo.task(conn, job["task_id"], True)
            if not await self._owns_lease(conn, job):
                return
            await conn.execute(
                s.jobs.update()
                .where(s.jobs.c.id == job["job_id"], s.jobs.c.status == "running")
                .values(status="failed" if failed else "done", lease_owner=None, lease_until=None)
            )
            await conn.execute(
                s.tasks.update()
                .where(s.tasks.c.id == task["id"])
                .values(lease_owner=None, lease_until=None)
            )
            if (
                failed
                and task["input_version"] == job["input_version"]
                and task["state"] in ("RUNNING", "READY")
            ):
                await conn.execute(
                    s.tasks.update()
                    .where(s.tasks.c.id == task["id"])
                    .values(state="PAUSED", wait_reason="worker_error")
                )
                await self.repo.emit(
                    conn,
                    task["id"],
                    job["owner_id"],
                    "task_state",
                    {"state": "PAUSED", "wait_reason": "worker_error"},
                )
            if task["input_version"] > job["input_version"] and task["state"] == "READY":
                await self.repo.enqueue(conn, task["id"], "run_turn", task["input_version"])

    async def reconcile(self, job):
        owner, task_id = job["owner_id"], job["task_id"]
        # Intent is durable BEFORE the separate business transaction.
        async with self.db.transaction(owner) as conn:
            task = await self.repo.task(conn, task_id, True)
            if not await self._owns_lease(conn, job):
                raise DomainError("STALE_LEASE", "旧执行者不得提交或对账")
            action = dict(
                (
                    await conn.execute(
                        sa.select(s.actions)
                        .where(s.actions.c.id == job["target_id"], s.actions.c.task_id == task_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if action["status"] in ("accepted", "failed"):
                return
            submit = action["status"] == "confirmed"
            stale_product = action["draft"].get("product_version") != task.get("product_version")
            if submit and (task.get("stop_requested") or stale_product):
                # A confirmation is not an issued business request. Already
                # submitting/unknown actions always proceed to reconciliation.
                reason: str | None = (
                    "user_stopped" if task.get("stop_requested") else "draft_product_changed"
                )
                await conn.execute(
                    s.actions.update()
                    .where(s.actions.c.id == action["id"])
                    .values(
                        status="failed",
                        receipt={"status": "not_submitted", "reason": reason, "is_simulated": True},
                    )
                )
                released = await self._release_blocked_turn(conn, task)
                state = (
                    "CANCELLED"
                    if task.get("stop_requested")
                    else "READY"
                    if released
                    else task["state"]
                )
                await conn.execute(
                    s.tasks.update()
                    .where(s.tasks.c.id == task_id)
                    .values(state=state, wait_reason=reason)
                )
                await self.repo.emit(
                    conn,
                    task_id,
                    owner,
                    "receipt",
                    {"status": "not_submitted", "reason": reason, "is_simulated": True},
                )
                return
            if submit:
                await conn.execute(
                    s.actions.update()
                    .where(s.actions.c.id == action["id"])
                    .values(status="submitting")
                )
        if submit:
            async with self.db.transaction(owner) as conn:
                latest = await self.repo.task(conn, task_id, True)
                if not await self._owns_lease(conn, job):
                    raise DomainError("STALE_LEASE", "旧执行者不得发出业务写入")
                if latest.get("stop_requested") or action["draft"].get(
                    "product_version"
                ) != latest.get("product_version"):
                    await conn.execute(
                        s.actions.update()
                        .where(s.actions.c.id == action["id"])
                        .values(
                            status="failed",
                            receipt={
                                "status": "not_submitted",
                                "reason": "task_changed_before_send",
                            },
                        )
                    )
                    await self._release_blocked_turn(conn, latest)
                    await self.repo.emit(
                        conn,
                        task_id,
                        owner,
                        "receipt",
                        {
                            "status": "not_submitted",
                            "reason": "task_changed_before_send",
                            "is_simulated": True,
                        },
                    )
                    return
                receipt = {
                    "status": "accepted",
                    "label": "未解决、已升级",
                    "is_simulated": True,
                    "recipient": action["draft"]["recipient"],
                    "operation_id": action["operation_id"],
                    "unresolved": True,
                }
                await conn.execute(
                    insert(s.tickets)
                    .values(
                        id=uid(), task_id=task_id, operation_id=action["operation_id"], data=receipt
                    )
                    .on_conflict_do_nothing(index_elements=["operation_id"])
                )
            if self.settings.fault_drop_receipt:
                await self.unknown_action(job, action)
                return
        # Query by the original operation id, never resubmit an unknown operation.
        async with self.db.transaction(owner) as conn:
            task = await self.repo.task(conn, task_id, True)
            if not await self._owns_lease(conn, job):
                return
            ticket = (
                (
                    await conn.execute(
                        sa.select(s.tickets).where(
                            s.tickets.c.operation_id == action["operation_id"]
                        )
                    )
                )
                .mappings()
                .first()
            )
            if ticket:
                receipt = {"id": ticket["id"], **ticket["data"]}
                await conn.execute(
                    s.actions.update()
                    .where(s.actions.c.id == action["id"])
                    .values(status="accepted", receipt=receipt)
                )
                released = await self._release_blocked_turn(conn, task)
                state = "READY" if released or task["state"] == "READY" else "WAITING"
                reason = None if state == "READY" else "human_service"
                await conn.execute(
                    s.tasks.update()
                    .where(s.tasks.c.id == task_id)
                    .values(state=state, wait_reason=reason, revision=s.tasks.c.revision + 1)
                )
                await self.repo.emit(conn, task_id, owner, "receipt", receipt)
                await self.repo.emit(
                    conn, task_id, owner, "task_state", {"state": state, "wait_reason": reason}
                )
                return
        await self.unknown_action(job, action)

    async def unknown_action(self, job, action):
        async with self.db.transaction(job["owner_id"]) as conn:
            task = await self.repo.task(conn, job["task_id"], True)
            if not await self._owns_lease(conn, job):
                return
            attempt = action["attempts"] + 1
            await conn.execute(
                s.actions.update()
                .where(s.actions.c.id == action["id"])
                .values(status="unknown", attempts=attempt)
            )
            await conn.execute(
                s.tasks.update()
                .where(s.tasks.c.id == task["id"])
                .values(state="PAUSED", wait_reason="action_unknown")
            )
            await self.repo.emit(
                conn,
                task["id"],
                job["owner_id"],
                "receipt",
                {"status": "unknown", "operation_id": action["operation_id"], "is_simulated": True},
            )
            if attempt <= 3:
                await self.repo.enqueue(
                    conn,
                    task["id"],
                    "reconcile_action",
                    attempt,
                    action["id"],
                    delay=(5, 30, 120)[attempt - 1],
                )

    async def _owns_lease(self, conn, job):
        return bool(
            (
                await conn.execute(
                    sa.select(s.tasks.c.id).where(
                        s.tasks.c.id == job["task_id"],
                        s.tasks.c.lease_epoch == job["lease_epoch"],
                        s.tasks.c.lease_owner == self.worker_id,
                        s.tasks.c.lease_until > sa.func.now(),
                    )
                )
            ).first()
        )

    async def _release_blocked_turn(self, conn, task):
        if task.get("stop_requested"):
            return False
        released = (
            await conn.execute(
                s.jobs.update()
                .where(
                    s.jobs.c.task_id == task["id"],
                    s.jobs.c.kind == "run_turn",
                    s.jobs.c.input_version == task["input_version"],
                    s.jobs.c.status == "blocked",
                )
                .values(status="queued", available_at=sa.func.now())
                .returning(s.jobs.c.id)
            )
        ).first()
        return bool(released)

    async def retire_task_content(self, job):
        """Tombstone thirty-day raw content, retaining minimal provenance.

        Waiting/active tasks, issued actions and unsettled model calls block it.
        The Worker has read-only originals; the maintenance CLI purges bytes.
        """
        owner, task_id = job["owner_id"], job["task_id"]
        async with self.db.transaction(owner) as conn:
            task = await self.repo.task(conn, task_id, True)
            if not await self._owns_lease(conn, job):
                return False
            due = (
                await conn.execute(
                    sa.select(s.tasks.c.id).where(
                        s.tasks.c.id == task_id,
                        s.tasks.c.state.in_(["COMPLETED", "CANCELLED"]),
                        s.tasks.c.completed_at <= sa.func.now() - sa.text("interval '30 days'"),
                    )
                )
            ).first()
            unresolved = (
                await conn.execute(
                    sa.select(s.actions.c.id).where(
                        s.actions.c.task_id == task_id,
                        s.actions.c.status.in_(["confirmed", "submitting", "unknown"]),
                    )
                )
            ).first()
            in_flight = (
                await conn.execute(
                    sa.select(s.model_calls.c.id).where(
                        s.model_calls.c.task_id == task_id, s.model_calls.c.status == "reserved"
                    )
                )
            ).first()
            if not due or unresolved or in_flight:
                return False
            uploads = (
                (
                    await conn.execute(
                        sa.select(s.artifacts)
                        .where(
                            s.artifacts.c.task_id == task_id,
                            s.artifacts.c.kind.in_(["upload", "deleted_upload"]),
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            for row in uploads:
                await conn.execute(
                    s.artifacts.update()
                    .where(s.artifacts.c.id == row["id"])
                    .values(
                        kind="deleted_upload",
                        data={
                            "storage_key": row["data"]["storage_key"],
                            "status": "deleted",
                            "retained": False,
                            "purge_pending": True,
                        },
                    )
                )
            rows = (
                (await conn.execute(sa.select(s.events).where(s.events.c.task_id == task_id)))
                .mappings()
                .all()
            )
            for event in rows:
                payload = {
                    key: event["payload"][key]
                    for key in (
                        "schema_version",
                        "role",
                        "event_type",
                        "step_instance_id",
                        "product_version",
                        "feedback",
                        "state",
                        "wait_reason",
                        "status",
                        "operation_id",
                    )
                    if key in event["payload"]
                }
                payload.update(retained=False, text="")
                await conn.execute(
                    s.events.update().where(s.events.c.id == event["id"]).values(payload=payload)
                )
            receipts = (
                (await conn.execute(sa.select(s.tickets).where(s.tickets.c.task_id == task_id)))
                .mappings()
                .all()
            )
            for receipt in receipts:
                data = {
                    key: receipt["data"][key]
                    for key in ("status", "operation_id", "is_simulated", "unresolved")
                    if key in receipt["data"]
                }
                await conn.execute(
                    s.tickets.update()
                    .where(s.tickets.c.id == receipt["id"])
                    .values(data={**data, "retained": False})
                )
            await conn.execute(
                s.actions.update()
                .where(s.actions.c.task_id == task_id)
                .values(draft={"retained": False}, receipt={"retained": False})
            )
            for key in (
                "messages",
                "pending_decision",
                "current_card",
                "current_step",
                "draft",
                "purchase",
                "warranty",
                "last_user_message",
                "service_signals",
                "service_intent",
                "image_observations",
                "image_attempts",
                "purchase_lookup",
                "urgency",
                "urgency_source_event_id",
                "create_body",
                "evidence",
            ):
                task.pop(key, None)
            task.update(
                goal="原始会话内容已按保留期限回收",
                todo={},
                content_retired=True,
                raw_files_pending_purge=bool(uploads),
            )
            task["step_history"] = [
                {
                    key: step.get(key)
                    for key in (
                        "step_id",
                        "step_instance_id",
                        "product_id",
                        "product_version",
                        "device_id",
                        "feedback",
                    )
                }
                for step in task.get("step_history", [])
            ]
            task["steps"] = task["step_history"]
            await self.repo.save_work(conn, task)
            await conn.execute(
                s.tasks.update().where(s.tasks.c.id == task_id).values(title="已归档售后任务")
            )
        return True

    async def extract_memory(self, task, events):
        if task.get("principal_role") != "consumer" or task["state"] not in (
            "COMPLETED",
            "CANCELLED",
        ):
            return
        from kefu.plugins.memory import MemoryPlugin
        from kefu.storage.memory import PostgresMemoryRepository

        messages = [event for event in events if event["payload"].get("role") == "user"]
        memory = MemoryPlugin(PostgresMemoryRepository(self.db))
        # Deterministic records preserve source words, with no paid inference or guessed preference.
        if messages and messages[-1]["payload"].get("text", "").strip():
            source = messages[-1]
            await memory.remember(
                user_id=task["owner_id"],
                content=source["payload"]["text"].strip()[:2000],
                source_event_ids=[source["id"]],
                memory_type="service_event",
                device_id=task.get("device_id"),
                product_id=(task.get("product") or {}).get("id"),
                facts={"task_id": task["id"], "state": task["state"]},
            )
        async with self.db.transaction(task["owner_id"], "read") as conn:
            feedback_events = (
                (
                    await conn.execute(
                        sa.select(s.events).where(
                            s.events.c.task_id == task["id"],
                            s.events.c.type == "user_event",
                            s.events.c.payload["event_type"].astext == "step_feedback",
                        )
                    )
                )
                .mappings()
                .all()
            )
        for event in feedback_events:
            payload = event["payload"]
            step = next(
                (
                    entry
                    for entry in task.get("step_history", [])
                    if entry.get("step_instance_id") == payload.get("step_instance_id")
                    and entry.get("product_version") == payload.get("product_version")
                ),
                None,
            )
            if not step:
                continue
            same_product = step.get("product_version") == task.get("product_version")
            product_id = step.get("product_id") or (
                (task.get("product") or {}).get("id") if same_product else None
            )
            device_id = step.get("device_id") or (task.get("device_id") if same_product else None)
            if not product_id or not device_id:
                continue
            content = f"用户对步骤 {step['step_id']} 的反馈：{payload.get('feedback')}。{payload.get('text', '')}"
            await memory.remember(
                user_id=task["owner_id"],
                content=content[:2000],
                source_event_ids=[event["id"]],
                memory_type="service_event",
                evidence_kind="user_report",
                device_id=device_id,
                product_id=product_id,
                facts={
                    "task_id": task["id"],
                    "step_id": step["step_id"],
                    "feedback": payload.get("feedback"),
                    "product_id": product_id,
                    "product_version": step.get("product_version"),
                },
            )

    async def run(self):
        async def subscribe():
            redis = Redis.from_url(
                self.settings.redis_url.get_secret_value(),
                socket_connect_timeout=1,
                socket_timeout=2,
            )
            try:
                while not self.stopping.is_set():
                    try:
                        async with redis.pubsub() as subscription:
                            await subscription.subscribe("kefu:wakeup")
                            while not self.stopping.is_set():
                                event = await subscription.get_message(
                                    ignore_subscribe_messages=True, timeout=1
                                )
                                if event:
                                    self.wakeup.set()
                    except RedisError:
                        # Durable job polling remains active while Redis recovers.
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(
                                self.stopping.wait(), timeout=self.settings.poll_seconds
                            )
            finally:
                await redis.aclose()

        async def slot():
            while not self.stopping.is_set():
                try:
                    if await self.process_one():
                        continue
                except Exception as exc:
                    log.error("worker_poll_failed error=%s", type(exc).__name__)
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=self.settings.poll_seconds)
                    self.wakeup.clear()
                except TimeoutError:
                    pass

        listener = asyncio.create_task(subscribe())
        try:
            await asyncio.gather(*(slot() for _ in range(self.settings.worker_concurrency)))
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

    async def close(self):
        await self.services.plugins.close()
        if hasattr(self.model, "close"):
            await self.model.close()
        await self.db.close()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = Worker()

    def stop(*_):
        worker.stopping.set()
        worker.wakeup.set()

    for kind in (signal.SIGINT, signal.SIGTERM):
        signal.signal(kind, stop)
    try:
        await worker.run()
    finally:
        await worker.close()


if __name__ == "__main__":
    configure_asyncio()
    asyncio.run(main())
