from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Contract):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


class Register(Contract):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name", mode="before")
    @classmethod
    def trim_display_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class CreateTask(Contract):
    create_key: str = Field(min_length=8, max_length=128)
    title: str = Field(default="新的售后咨询", min_length=1, max_length=200)
    region: str = Field(default="CN", max_length=24)
    channel: str = Field(default="unknown", max_length=100)


class MessageInput(Contract):
    client_event_id: str = Field(min_length=8, max_length=128)
    text: str = Field(default="", max_length=16000)
    material_ids: list[str] = Field(default_factory=list, max_length=3)
    timezone: str = "Asia/Shanghai"

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
        return value


class EventInput(Contract):
    client_event_id: str = Field(min_length=8, max_length=128)
    type: Literal[
        "product_confirmed",
        "purchase_selected",
        "product_rejected",
        "product_correction",
        "step_feedback",
        "escalation_confirmed",
        "stop",
        "resume",
    ]
    expected_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class ProductConfirmation(Contract):
    card_id: str
    card_version: int
    product_id: str
    device_id: str | None = None


class PurchaseSelection(Contract):
    purchase_id: str = Field(min_length=1, max_length=200)


class CardRejection(Contract):
    card_id: str
    card_version: int


class StepFeedback(Contract):
    step_instance_id: str
    product_version: int
    feedback: Literal["performed", "no_change", "unable", "recovered"]
    text: str = Field(default="", max_length=2000)


class EscalationConfirmation(Contract):
    draft_id: str
    draft_version: int


class StaffUpdate(Contract):
    client_event_id: str = Field(min_length=8, max_length=128)
    type: Literal["progress", "request_info", "advice", "resolved", "internal_note"]
    text: str = Field(min_length=1, max_length=8000)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    expected_version: int = Field(ge=0)


class MemoryCorrection(Contract):
    client_event_id: str = Field(min_length=8, max_length=128)
    text: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=1)


class MemoryForget(Contract):
    client_event_id: str = Field(min_length=8, max_length=128)
    expected_version: int = Field(ge=1)


class AdoptTasks(Contract):
    task_ids: list[str] = Field(min_length=1, max_length=100)
