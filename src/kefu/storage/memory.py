"""PostgreSQL memory with owner isolation and atomic source-version fences."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from kefu.plugins.memory import MemoryConflict
from kefu.storage import schema as s
from kefu.storage.repository import DomainError, Repository, now


class PostgresMemoryRepository:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.tasks = Repository(db)

    async def _lock_owner(self, conn: Any, user_id: str) -> None:
        # One stable advisory lock serializes source registration, extraction and
        # deletion for a user, including sources which do not yet have a state row.
        await conn.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"),
            {"owner": "memory:" + user_id},
        )

    def _record_deletion(
        self, user_id: str, memory_id: str, sources: list[str], status: str, version: int
    ) -> None:
        """Write-ahead exclusion ledger survives restoring an older database backup.

        A transaction rollback can leave an extra conservative exclusion in the ledger.
        That is preferable to resurrecting forgotten content during disaster recovery.
        """
        path = Path(self.db.settings.deletion_ledger_path)
        payload = (
            json.dumps(
                {
                    "owner_id": user_id,
                    "memory_id": memory_id,
                    "source_ids": sources,
                    "status": status,
                    "version": version,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                encoded = payload.encode("utf-8")
                written = os.write(descriptor, encoded)
                if written != len(encoded):
                    raise OSError("Incomplete deletion ledger write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise DomainError(
                "DELETION_LEDGER_UNAVAILABLE", "遗忘清单暂不可写，变更未提交，请稍后重试", 503, True
            ) from exc

    async def _states(
        self, conn: Any, user_id: str, source_ids: list[str], lock: bool = False
    ) -> dict[str, dict[str, Any]]:
        if not source_ids:
            return {}
        query = (
            sa.select(s.events, s.tasks.c.owner_id)
            .join(s.tasks, s.tasks.c.id == s.events.c.task_id)
            .where(s.tasks.c.owner_id == user_id, s.events.c.id.in_(source_ids))
        )
        events = (await conn.execute(query)).mappings().all()
        for event in events:
            await conn.execute(
                insert(s.source_states)
                .values(owner_id=user_id, source_id=event["id"], version=1, excluded=False)
                .on_conflict_do_nothing(index_elements=["owner_id", "source_id"])
            )
        state_query = (
            sa.select(s.source_states)
            .where(
                s.source_states.c.owner_id == user_id, s.source_states.c.source_id.in_(source_ids)
            )
            .order_by(s.source_states.c.source_id)
        )
        if lock:
            state_query = state_query.with_for_update()
        states = {row["source_id"]: row for row in (await conn.execute(state_query)).mappings()}
        result = {}
        for event in events:
            state = states[event["id"]]
            payload = event["payload"]
            result[event["id"]] = {
                "version": state["version"],
                "deleted": False,
                "memory_allowed": not state["excluded"],
                "user_id": user_id,
                "device_id": payload.get("device_id"),
                "evidence_kind": "tool_verified"
                if event["type"] in {"tool_result", "ticket_receipt", "purchase_verified"}
                else "user_report",
                "task_id": event["task_id"],
            }
        return result

    async def source_states(self, user_id: str, source_ids: list[str]) -> dict[str, dict[str, Any]]:
        async with self.db.transaction(user_id) as conn:
            await self._lock_owner(conn, user_id)
            return await self._states(conn, user_id, source_ids)

    async def list_memories(self, user_id: str) -> list[dict[str, Any]]:
        async with self.db.transaction(user_id) as conn:
            rows = (
                (await conn.execute(sa.select(s.memories).where(s.memories.c.owner_id == user_id)))
                .mappings()
                .all()
            )
            return [
                {
                    **copy.deepcopy(row["data"]),
                    "id": row["id"],
                    "user_id": row["owner_id"],
                    "device_id": row["device_id"],
                    "version": row["version"],
                }
                for row in rows
            ]

    async def _verify(
        self, conn: Any, record: dict[str, Any], expected: dict[str, int]
    ) -> dict[str, dict[str, Any]]:
        if set(expected) != set(record["source_event_ids"]):
            raise MemoryConflict("Every source must have a captured version")
        states = await self._states(conn, record["user_id"], record["source_event_ids"], lock=True)
        for source_id, version in expected.items():
            state = states.get(source_id)
            if state is None or not state["memory_allowed"] or state["version"] != version:
                raise MemoryConflict(
                    "Memory source changed, was excluded, or is not owned by the user"
                )
        return states

    async def commit_memory(
        self, record: dict[str, Any], expected_sources: dict[str, int]
    ) -> dict[str, Any]:
        owner = record["user_id"]
        async with self.db.transaction(owner) as conn:
            await self._lock_owner(conn, owner)
            states = await self._verify(conn, record, expected_sources)
            previous = (
                (
                    await conn.execute(
                        sa.select(s.memories)
                        .where(s.memories.c.id == record["id"], s.memories.c.owner_id == owner)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                old = previous["data"]
                if old["status"] != "active" or old["content"] != record["content"]:
                    raise MemoryConflict(
                        "Memory was invalidated or conflicts with its original source"
                    )
                return copy.deepcopy(old)
            saved = copy.deepcopy(record)
            saved["task_id"] = next(iter(states.values()))["task_id"]
            await conn.execute(
                s.memories.insert().values(
                    id=saved["id"],
                    owner_id=owner,
                    device_id=saved.get("device_id"),
                    version=saved["version"],
                    data=saved,
                )
            )
            return saved

    async def _invalidate(
        self,
        conn: Any,
        user_id: str,
        memory_id: str,
        expected_version: int,
        status: str,
        correction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"forgotten", "corrected", "invalidated"}:
            raise ValueError("Invalid memory transition")
        row = (
            (
                await conn.execute(
                    sa.select(s.memories)
                    .where(s.memories.c.id == memory_id, s.memories.c.owner_id == user_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DomainError("NOT_FOUND", "记忆不存在或不可访问", 404)
        original = copy.deepcopy(row["data"])
        if row["version"] != expected_version or original["status"] != "active":
            raise DomainError("VERSION_CONFLICT", "记忆已被更正或遗忘，请刷新")
        if correction:
            if correction["user_id"] != user_id or set(correction["source_event_ids"]) & set(
                original["source_event_ids"]
            ):
                raise MemoryConflict("Correction requires a distinct authorized source event")
            states = await self._verify(conn, correction, correction["source_versions"])
            correction["task_id"] = next(iter(states.values()))["task_id"]
        sources = original["source_event_ids"]
        await self._states(conn, user_id, sources, lock=True)
        self._record_deletion(user_id, memory_id, sources, status, expected_version + 1)
        await conn.execute(
            s.source_states.update()
            .where(s.source_states.c.owner_id == user_id, s.source_states.c.source_id.in_(sources))
            .values(
                version=s.source_states.c.version + 1,
                **({"excluded": True} if status == "forgotten" else {}),
            )
        )
        rows = (
            (
                await conn.execute(
                    sa.select(s.memories).where(s.memories.c.owner_id == user_id).with_for_update()
                )
            )
            .mappings()
            .all()
        )
        affected = []
        for other in rows:
            data = copy.deepcopy(other["data"])
            if data.get("status") != "active" or not set(sources).intersection(
                data.get("source_event_ids", [])
            ):
                continue
            data["status"] = status if other["id"] == memory_id else "invalidated"
            data["version"] = other["version"] + 1
            if status == "forgotten":
                data["content"] = ""
                data["facts"] = {}
                data["conditions"] = {}
            await conn.execute(
                s.memories.update()
                .where(s.memories.c.id == other["id"], s.memories.c.owner_id == user_id)
                .values(version=data["version"], data=data)
            )
            affected.append(other["id"])
            if other["id"] == memory_id:
                original = data
        if status == "forgotten":
            await conn.execute(
                insert(s.memory_exclusions)
                .values(
                    id="forget-" + memory_id,
                    owner_id=user_id,
                    data={
                        "memory_id": memory_id,
                        "source_event_ids": sources,
                        "affected_ids": affected,
                    },
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
        if correction:
            await conn.execute(
                s.memories.insert().values(
                    id=correction["id"],
                    owner_id=user_id,
                    device_id=correction.get("device_id"),
                    version=correction["version"],
                    data=correction,
                )
            )
        # Invalidate projections before returning. Raw evidence has an independent retention policy.
        await conn.execute(
            s.artifacts.update()
            .where(s.artifacts.c.owner_id == user_id, s.artifacts.c.kind == "context")
            .values(data=s.artifacts.c.data.op("||")({"status": "invalidated"}))
        )
        # A response already in flight with old memory must fail its input-version fence.
        changed_tasks = (
            (
                await conn.execute(
                    s.tasks.update()
                    .where(
                        s.tasks.c.owner_id == user_id,
                        s.tasks.c.state.in_(["READY", "RUNNING", "WAITING", "PAUSED"]),
                    )
                    .values(
                        state=sa.case(
                            (s.tasks.c.state == "RUNNING", "READY"), else_=s.tasks.c.state
                        ),
                        input_version=s.tasks.c.input_version + 1,
                        revision=s.tasks.c.revision + 1,
                        updated_at=sa.func.now(),
                    )
                    .returning(s.tasks.c.id, s.tasks.c.state, s.tasks.c.input_version)
                )
            )
            .mappings()
            .all()
        )
        for task in changed_tasks:
            await self.tasks.emit(
                conn,
                task["id"],
                user_id,
                "memory_changed",
                {"memory_id": memory_id, "status": status, "input_version": task["input_version"]},
            )
            if task["state"] == "READY":
                await self.tasks.enqueue(conn, task["id"], "run_turn", task["input_version"])
        return original

    async def invalidate_memory(
        self,
        user_id: str,
        memory_id: str,
        expected_version: int,
        status: str,
        correction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.db.transaction(user_id) as conn:
            await self._lock_owner(conn, user_id)
            return await self._invalidate(
                conn, user_id, memory_id, expected_version, status, correction
            )

    async def change(
        self,
        user_id: str,
        memory_id: str,
        expected_version: int,
        text: str | None,
        client_event_id: str,
    ) -> dict[str, Any]:
        """Atomic API correction/forget command, including durable source event and replay key."""
        if not client_event_id or len(client_event_id) > 128:
            raise DomainError("INVALID_INPUT", "需要有效的请求编号", 422)
        if text is not None and (not text.strip() or len(text) > 16000):
            raise DomainError("INVALID_INPUT", "更正内容须为 1–16000 个字符", 422)
        async with self.db.transaction(user_id) as conn:
            await self._lock_owner(conn, user_id)
            row = (
                (
                    await conn.execute(
                        sa.select(s.memories).where(
                            s.memories.c.id == memory_id, s.memories.c.owner_id == user_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise DomainError("NOT_FOUND", "记忆不存在或不可访问", 404)
            original = copy.deepcopy(row["data"])
            source_task = original.get("task_id")
            if not source_task:
                source_task = (
                    await conn.execute(
                        sa.select(s.events.c.task_id)
                        .join(s.tasks, s.tasks.c.id == s.events.c.task_id)
                        .where(
                            s.tasks.c.owner_id == user_id,
                            s.events.c.id.in_(original["source_event_ids"]),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if source_task is None:
                raise DomainError("SOURCE_UNAVAILABLE", "无法核验原始来源", 409)
            await self.tasks.task(conn, source_task, lock=True)
            request = {"memory_id": memory_id, "expected_version": expected_version, "text": text}
            repeated = await self.tasks.repeated(
                conn, user_id, source_task, client_event_id, request
            )
            if repeated is not None:
                return repeated
            if row["version"] != expected_version or original["status"] != "active":
                raise DomainError("VERSION_CONFLICT", "记忆已更新，请刷新")
            status = "forgotten" if text is None else "corrected"
            event = await self.tasks.emit(
                conn,
                source_task,
                user_id,
                "memory_" + status,
                {"memory_id": memory_id, "text": text},
            )
            replacement = None
            if text is not None:
                replacement = {
                    **original,
                    "id": "mem-" + hashlib.sha256(event["id"].encode()).hexdigest()[:24],
                    "content": text,
                    "source_event_ids": [event["id"]],
                    "source_versions": {event["id"]: 1},
                    "evidence_kind": "user_report",
                    "version": 1,
                    "status": "active",
                    "supersedes_id": memory_id,
                    "occurred_at": now().isoformat(),
                    "facts": {},
                    "reviewed_by": None,
                    "needs_review": original["type"] == "experience",
                    "task_id": source_task,
                    "device_id": None
                    if original["type"] == "service_event"
                    else original.get("device_id"),
                    "needs_device_clarification": original["type"] == "service_event",
                }
            changed = await self._invalidate(
                conn, user_id, memory_id, expected_version, status, replacement
            )
            response = {
                "memory": replacement or changed,
                "previous_memory_id": memory_id,
                "event_id": event["id"],
                "status": status,
            }
            await self.tasks.remember_request(
                conn, user_id, source_task, client_event_id, request, response
            )
            return response


# Public name mirrors the plugin repository protocol without adding another implementation.
MemoryRepository = PostgresMemoryRepository
