"""Source-bound personal memory. Every mutation is authorized and atomic."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from kefu.plugins.knowledge import result, scope_check


class MemoryRepository(Protocol):
    async def list_memories(self, user_id: str) -> list[dict[str, Any]]: ...

    async def source_states(self, user_id: str, source_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Each state: version:int, deleted:bool, memory_allowed:bool, user_id:str."""
        ...

    async def commit_memory(
        self, record: dict[str, Any], expected_sources: dict[str, int]
    ) -> dict[str, Any]:
        """Under a transaction lock recheck every source version/tombstone; dedupe id."""
        ...

    async def invalidate_memory(
        self,
        user_id: str,
        memory_id: str,
        expected_version: int,
        status: str,
        correction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invalidate old record and derived rows, bump its source versions to fence
        stale extraction; on forget set source memory_allowed=False. A correction
        requires separate current source events, and is saved in the same transaction.
        """
        ...


class MemoryConflict(ValueError):
    """A source/version changed while a background extraction was running."""


class InMemoryMemoryRepository:
    """Test implementation of the same atomic fence required of PostgreSQL."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def list_memories(self, user_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy([r for r in self.records.values() if r["user_id"] == user_id])

    async def source_states(self, user_id: str, source_ids: list[str]) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(
            {
                sid: self.sources[sid]
                for sid in source_ids
                if sid in self.sources and self.sources[sid]["user_id"] == user_id
            }
        )

    def _validate_sources(self, record: dict[str, Any], expected_sources: dict[str, int]) -> None:
        if set(expected_sources) != set(record["source_event_ids"]):
            raise MemoryConflict("All sources need a version fence")
        for source_id, expected in expected_sources.items():
            source = self.sources.get(source_id)
            if (
                source is None
                or source["user_id"] != record["user_id"]
                or source.get("deleted")
                or not source.get("memory_allowed", True)
                or source["version"] != expected
            ):
                raise MemoryConflict("Source no longer authorizes memory extraction")

    async def commit_memory(
        self, record: dict[str, Any], expected_sources: dict[str, int]
    ) -> dict[str, Any]:
        async with self._lock:
            self._validate_sources(record, expected_sources)
            previous = self.records.get(record["id"])
            if previous:
                if previous["status"] != "active" or previous["content"] != record["content"]:
                    raise MemoryConflict(
                        "Memory was invalidated or its source now has conflicting interpretations"
                    )
                return copy.deepcopy(previous)
            self.records[record["id"]] = copy.deepcopy(record)
            return copy.deepcopy(record)

    async def invalidate_memory(
        self,
        user_id: str,
        memory_id: str,
        expected_version: int,
        status: str,
        correction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            original = self.records.get(memory_id)
            if original is None or original["user_id"] != user_id:
                raise PermissionError("Memory unavailable")
            if original["version"] != expected_version or original["status"] != "active":
                raise MemoryConflict("Memory version changed")
            if correction:
                self._validate_sources(correction, correction["source_versions"])
                if correction["user_id"] != user_id or set(correction["source_event_ids"]) & set(
                    original["source_event_ids"]
                ):
                    raise MemoryConflict("Correction needs its own new source event")
            for source_id in original["source_event_ids"]:
                source = self.sources[source_id]
                source["version"] += 1
                if status == "forgotten":
                    source["memory_allowed"] = False
            # Derived records sharing these sources are also no longer trustworthy.
            source_ids = set(original["source_event_ids"])
            for other in self.records.values():
                if (
                    other["user_id"] == user_id
                    and source_ids & set(other["source_event_ids"])
                    and other["status"] == "active"
                ):
                    other["status"] = status if other["id"] == memory_id else "invalidated"
                    other["version"] += 1
                    if status == "forgotten":
                        other["content"] = ""
                        other["facts"] = {}
                        other["conditions"] = {}
            if correction:
                self.records[correction["id"]] = copy.deepcopy(correction)
            return copy.deepcopy(original)


def _instant(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


class MemoryPlugin:
    TYPES = frozenset({"service_event", "preference", "experience"})
    EVIDENCE_KINDS = frozenset({"user_report", "tool_verified", "model_inference"})

    def __init__(
        self, repository: MemoryRepository, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    async def prepare_candidate(
        self,
        user_id: str,
        content: str,
        source_event_ids: list[str],
        memory_type: str = "service_event",
        device_id: str | None = None,
        problem_id: str | None = None,
        evidence_kind: str = "user_report",
        occurred_at: str | None = None,
        valid_until: str | None = None,
        product_id: str | None = None,
        conditions: dict[str, Any] | None = None,
        reviewed_by: str | None = None,
        supersedes_id: str | None = None,
        facts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if memory_type not in self.TYPES or evidence_kind not in self.EVIDENCE_KINDS:
            raise ValueError("Unsupported memory or evidence kind")
        if not content.strip() or not source_event_ids or len(content) > 16000:
            raise ValueError("Memory needs bounded content and source events")
        if memory_type == "preference" and evidence_kind != "user_report":
            raise ValueError("A stable preference must be explicitly expressed by the user")
        if memory_type == "experience" and (not reviewed_by or not product_id or not conditions):
            raise ValueError(
                "Reusable experience needs a reviewer, product, and explicit applicability conditions"
            )
        sources = await self.repository.source_states(user_id, source_event_ids)
        if set(sources) != set(source_event_ids):
            raise PermissionError("Some source events are not accessible")
        for source in sources.values():
            if source.get("deleted") or not source.get("memory_allowed", True):
                raise MemoryConflict("Source is excluded from memory")
            if source.get("device_id") and device_id and source["device_id"] != device_id:
                raise ValueError("Memory device conflicts with its source")
            if evidence_kind == "tool_verified" and source.get("evidence_kind") != "tool_verified":
                raise ValueError(
                    "User statements and model inference cannot become tool-verified facts"
                )
        occurred_at = occurred_at or self.clock().isoformat()
        _instant(occurred_at)
        if valid_until is not None and _instant(valid_until) <= _instant(occurred_at):
            raise ValueError("Memory validity interval is empty")
        key = json.dumps(
            [
                user_id,
                memory_type,
                sorted(set(source_event_ids)),
                device_id,
                problem_id,
                supersedes_id,
            ]
        )
        return {
            "id": "mem-" + hashlib.sha256(key.encode()).hexdigest()[:24],
            "type": memory_type,
            "content": content,
            "user_id": user_id,
            "device_id": device_id,
            "problem_id": problem_id,
            "source_event_ids": sorted(set(source_event_ids)),
            "evidence_kind": evidence_kind,
            "source_versions": {sid: state["version"] for sid, state in sources.items()},
            "occurred_at": occurred_at,
            "valid_until": valid_until,
            "version": 1,
            "status": "active",
            "supersedes_id": supersedes_id,
            "product_id": product_id,
            "conditions": conditions or {},
            "reviewed_by": reviewed_by,
            "facts": copy.deepcopy(facts or {}),
            "needs_device_clarification": memory_type == "service_event" and device_id is None,
        }

    async def write(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Call with a server-validated candidate produced by prepare_candidate."""
        return await self.repository.commit_memory(candidate, candidate["source_versions"])

    async def remember(self, **kwargs: Any) -> dict[str, Any]:
        return await self.write(await self.prepare_candidate(**kwargs))

    async def recall(
        self,
        user_id: str,
        device_id: str | None = None,
        problem_id: str | None = None,
        product_id: str | None = None,
        conditions: dict[str, Any] | None = None,
        limit: int = 12,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ValueError("Recall limit must be 1 through 50")
        now = _instant(as_of) if as_of else self.clock()
        memories, pending = [], []
        for record in await self.repository.list_memories(user_id):
            if record["user_id"] != user_id or record["status"] != "active":
                continue
            if _instant(record["occurred_at"]) > now or (
                record.get("valid_until") and _instant(record["valid_until"]) <= now
            ):
                continue
            # Same model is not sufficient to identify an individual physical device.
            if record["type"] == "service_event":
                if not device_id or record.get("device_id") != device_id:
                    continue
                if problem_id and record.get("problem_id") != problem_id:
                    continue
            states = await self.repository.source_states(user_id, record["source_event_ids"])
            if any(
                sid not in states
                or states[sid].get("deleted")
                or not states[sid].get("memory_allowed", True)
                or states[sid]["version"] != record["source_versions"][sid]
                for sid in record["source_event_ids"]
            ):
                continue
            item = copy.deepcopy(record)
            if record["type"] == "experience":
                if not record.get("reviewed_by") or record.get("needs_review"):
                    continue
                applicable, missing = scope_check(
                    {"product_ids": [record.get("product_id")], "conditions": record["conditions"]},
                    {"product_id": product_id, "conditions": conditions or {}},
                )
                if not applicable:
                    continue
                if missing:
                    item["missing_conditions"] = missing
                    item["usable_for_operation"] = False
                    pending.append(item)
                    continue
            item["usable_for_operation"] = (
                False  # Current approved knowledge still governs physical steps.
            )
            memories.append(item)
        # Failed steps before other history; all records retain their original time/result.
        memories.sort(
            key=lambda r: (
                r.get("facts", {}).get("feedback") in {"no_change", "unable"},
                r["occurred_at"],
            ),
            reverse=True,
        )
        memories = memories[:limit]
        return result(
            "ok",
            {
                "memories": memories,
                "pending_conditions": pending[:limit],
                "device_confirmation_required": device_id is None,
            },
            sorted({sid for r in memories for sid in r["source_event_ids"]}),
        )

    async def forget(self, user_id: str, memory_id: str, expected_version: int) -> dict[str, Any]:
        return await self.repository.invalidate_memory(
            user_id, memory_id, expected_version, "forgotten"
        )

    async def correct(
        self,
        user_id: str,
        memory_id: str,
        expected_version: int,
        content: str,
        source_event_ids: list[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        records = await self.repository.list_memories(user_id)
        original = next((r for r in records if r["id"] == memory_id), None)
        if original is None:
            raise PermissionError("Memory unavailable")
        args: dict[str, Any] = {
            key: original.get(key)
            for key in (
                "device_id",
                "problem_id",
                "evidence_kind",
                "product_id",
                "conditions",
                "facts",
            )
        }
        args.update(kwargs)
        args["memory_type"] = original["type"]
        args["supersedes_id"] = memory_id
        candidate = await self.prepare_candidate(user_id, content, source_event_ids, **args)
        await self.repository.invalidate_memory(
            user_id, memory_id, expected_version, "corrected", candidate
        )
        return candidate
