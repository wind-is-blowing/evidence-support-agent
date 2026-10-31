"""Small host interfaces: persistence and identity stay outside the model loop."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class TaskState(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@dataclass
class TurnResult:
    task: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None


class RuntimePorts(Protocol):
    """All hooks run with the server's authenticated task/lease context.

    ``version`` is the input/goal revision, never changed by model tools.
    Checkpoint must atomically verify that revision and the worker lease, and
    merge trusted input fields rather than allowing a stale snapshot to win.
    A failed checkpoint raises; callers must not continue performing actions.
    ``call_tool`` must authorize every resource against task owner and scope.
    It implements the 8 service tools and read_context; TODO and skill loading
    are implemented by the executor. It may also implement load_skill if the
    host provides a packaged skill store. No tool is a business submit API.
    """

    async def is_current(self, task_id: str, version: int) -> bool: ...

    async def checkpoint(self, task: dict[str, Any], event: dict[str, Any]) -> None: ...

    async def reserve_budget(self, task_id: str, call_id: str, tokens: int) -> bool: ...

    async def settle_budget(self, call_id: str, usage: dict[str, Any]) -> None: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any], task: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def build_context(
        self, task: dict[str, Any], events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...
