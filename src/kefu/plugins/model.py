"""A single Qwen/OpenAI-compatible adapter and an explicitly offline demo.

No SDK types escape this module, no automatic retries, no partial tool calls.
"""

import asyncio
import ipaddress
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from .tools import validate_arguments


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    usage: dict[str, Any] = field(default_factory=lambda: {"status": "unknown"})
    request_id: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "ModelResponse":
        return cls(
            **{**value, "tool_calls": [ToolCall(**call) for call in value.get("tool_calls", [])]}
        )


@dataclass
class ModelRequest:
    task_id: str
    step_id: str
    call_id: str
    task_version: int
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)
    purpose: str = "agent"
    forced_tool: str | None = None
    config_version: str = "qwen-v1"
    max_output_tokens: int = 2048
    timeout_seconds: float = 45
    response_schema: dict | None = None
    # Task projection is for deterministic demo/testing; never sent to SDK.
    task_snapshot: dict = field(default_factory=dict)


class ModelError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: float = 0,
        usage: dict | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.usage = usage or {"status": "unknown"}


class Model(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, entry in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = entry
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def parse_json(value: str) -> Any:
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def validate_response(response: ModelResponse, allowed: set[str]) -> None:
    if response.finish_reason not in {"stop", "tool_calls"}:
        raise ModelError(
            "output_truncated" if response.finish_reason == "length" else "incomplete_response",
            usage=response.usage,
        )
    if response.tool_calls and response.finish_reason != "tool_calls":
        raise ModelError("inconsistent_finish_reason", usage=response.usage)
    if not response.tool_calls and response.finish_reason == "tool_calls":
        raise ModelError("inconsistent_finish_reason", usage=response.usage)
    ids: set[str] = set()
    for call in response.tool_calls:
        if not call.id or len(call.id) > 200 or call.id in ids or call.name not in allowed:
            raise ModelError("invalid_tool_call", usage=response.usage)
        ids.add(call.id)
        try:
            validate_arguments(call.name, call.arguments)
        except (ValueError, TypeError) as error:
            raise ModelError("invalid_tool_arguments", usage=response.usage) from error
    if not response.tool_calls and not response.text.strip():
        raise ModelError("empty_response", usage=response.usage)


def _validate_endpoint(base_url: str, provider: str) -> None:
    if provider not in {"dashscope", "ollama"}:
        raise ValueError("Unsupported model provider")
    if any(ord(char) <= 32 or ord(char) == 127 for char in base_url) or "\\" in base_url:
        raise ValueError("Invalid model endpoint URL")
    try:
        endpoint = urlsplit(base_url)
        host = endpoint.hostname
        port = endpoint.port
    except ValueError as error:
        raise ValueError("Invalid model endpoint URL") from error
    if (
        not host
        or endpoint.username is not None
        or endpoint.password is not None
        or "?" in base_url
        or "#" in base_url
        or "%" in host
        or port == 0
    ):
        raise ValueError("Invalid model endpoint URL")
    if endpoint.scheme == "https":
        return
    loopback = host.lower() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if provider != "ollama" or endpoint.scheme != "http" or not loopback:
        raise ValueError("Model endpoint requires HTTPS; Ollama HTTP must use a loopback address")


class QwenModel:
    """Capabilities must be explicitly configured/tested by deployment."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = "qwen3.5-35b-a3b",
        provider: Literal["dashscope", "ollama"] = "dashscope",
        enable_thinking: bool = False,
        supports_tool_choice: bool | None = None,
        supports_json_schema: bool | None = None,
        client: Any = None,
    ):
        if not api_key or not base_url or not model:
            raise ValueError("MODEL_API_KEY, MODEL_BASE_URL and MODEL_ID are required")
        _validate_endpoint(base_url, provider)
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=45)
        self.client = client
        self.model = model
        self.provider = provider
        self.enable_thinking = enable_thinking
        self.supports_tool_choice = (
            provider == "dashscope" if supports_tool_choice is None else supports_tool_choice
        )
        self.supports_json_schema = (
            provider == "ollama" if supports_json_schema is None else supports_json_schema
        )

    async def close(self) -> None:
        await self.client.close()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if request.purpose not in {"agent", "vision", "summary", "memory"}:
            raise ModelError("unsupported_purpose")
        if request.purpose != "agent" and request.tools:
            raise ModelError("auxiliary_tools_forbidden")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "stream": False,
            "temperature": 0,
            "max_tokens": request.max_output_tokens,
        }
        thinking = self.enable_thinking and request.purpose == "agent"
        if self.provider == "ollama":
            kwargs["reasoning_effort"] = "low" if thinking else "none"
        else:
            kwargs["extra_body"] = {"enable_thinking": thinking}
        if request.tools:
            kwargs["tools"] = request.tools
            if request.forced_tool and self.supports_tool_choice:
                kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": request.forced_tool},
                }
        if request.response_schema:
            if request.tools:
                raise ModelError("mixed_output_contracts")
            kwargs["response_format"] = (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.purpose,
                        "strict": True,
                        "schema": request.response_schema,
                    },
                }
                if self.supports_json_schema
                else {"type": "json_object"}
            )
        try:
            async with asyncio.timeout(request.timeout_seconds):
                raw = await self.client.chat.completions.create(**kwargs)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise ModelError("model_timeout", retryable=True) from error
        except Exception as error:
            status = getattr(error, "status_code", None)
            headers = getattr(getattr(error, "response", None), "headers", {})
            try:
                delay = max(0.0, float(headers.get("retry-after", "0")))
            except (ValueError, TypeError):
                try:
                    delay = max(
                        0.0,
                        (
                            parsedate_to_datetime(headers["retry-after"]) - datetime.now(UTC)
                        ).total_seconds(),
                    )
                except (ValueError, TypeError, KeyError):
                    delay = 0
            temporary = (
                status == 429
                or (isinstance(status, int) and status >= 500)
                or type(error).__name__ in {"APIConnectionError", "APITimeoutError"}
            )
            raise ModelError(
                "model_temporarily_unavailable" if temporary else "model_configuration_error",
                retryable=temporary,
                retry_after=delay,
            ) from error
        usage: dict[str, Any] = {"status": "unknown"}
        if getattr(raw, "usage", None) is not None:
            raw_usage = raw.usage
            usage = {
                "status": "actual",
                "input_tokens": getattr(raw_usage, "prompt_tokens", 0),
                "output_tokens": getattr(raw_usage, "completion_tokens", 0),
                "total_tokens": getattr(raw_usage, "total_tokens", None),
            }
            if usage["total_tokens"] is None:
                usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        choices = getattr(raw, "choices", [])
        if len(choices) != 1:
            raise ModelError("invalid_response_choices", usage=usage)
        choice = choices[0]
        if choice.finish_reason not in {"stop", "tool_calls"}:
            raise ModelError(
                "output_truncated" if choice.finish_reason == "length" else "incomplete_response",
                usage=usage,
            )
        calls = []
        try:
            for call in getattr(choice.message, "tool_calls", None) or []:
                arguments = parse_json(call.function.arguments)
                if not isinstance(arguments, dict):
                    raise TypeError("Tool arguments must be objects")
                calls.append(ToolCall(call.id, call.function.name, arguments))
        except (ValueError, TypeError, AttributeError) as error:
            raise ModelError("invalid_tool_arguments", usage=usage) from error
        response = ModelResponse(
            text=choice.message.content or "",
            tool_calls=calls,
            finish_reason=choice.finish_reason,
            model=getattr(raw, "model", self.model),
            usage=usage,
            request_id=getattr(raw, "_request_id", None) or getattr(raw, "id", None),
        )
        validate_response(response, {tool["function"]["name"] for tool in request.tools})
        if request.response_schema:
            try:
                value = parse_json(response.text)
                # JSON validation is mandatory even when provider enforces format.
                from .tools import _validate

                _validate(value, request.response_schema)
            except (ValueError, TypeError) as error:
                raise ModelError("invalid_auxiliary_json", usage=usage) from error
        return response


class DemoModel:
    """Offline deterministic demo; produces no claims of model effectiveness."""

    mode = "demo"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        task = request.task_snapshot
        query = task.get("last_user_message") or task.get("goal", "产品")
        usage = {"status": "simulated", "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        def call(name: str, args: dict) -> ModelResponse:
            return ModelResponse(
                tool_calls=[ToolCall(str(uuid4()), name, args)],
                finish_reason="tool_calls",
                model="offline-demo-rules-v1",
                usage=usage,
            )

        def say(content: str) -> ModelResponse:
            return ModelResponse(
                text="【离线演示】" + content, model="offline-demo-rules-v1", usage=usage
            )

        if request.purpose != "agent":
            raise ModelError("demo_auxiliary_unavailable")
        if request.forced_tool == "todo":
            plan = task.get("todo") or {"version": 0, "items": []}
            if (
                plan["items"]
                and task.get("plan_product_version") == task.get("product_version", 0)
                and task.get("plan_goal_version") == task.get("goal_version", 0)
            ):
                return call(
                    "todo",
                    {"mode": "review", "expected_version": plan["version"], "reviewed": True},
                )
            return call(
                "todo",
                {
                    "mode": "update",
                    "expected_version": plan["version"],
                    "items": [
                        {
                            "id": "identify",
                            "title": "确认产品与故障",
                            "status": "done" if task.get("product") else "in_progress",
                        },
                        {
                            "id": "diagnose",
                            "title": "查阅适用资料并记录操作反馈",
                            "status": "in_progress" if task.get("product") else "pending",
                        },
                        {
                            "id": "followup",
                            "title": "确认恢复或提交人工接续草案",
                            "status": "pending",
                        },
                    ],
                },
            )
        if request.forced_tool == "resolve_product":
            return call("resolve_product", {"query": query[:500]})
        if request.forced_tool == "search_knowledge":
            return call("search_knowledge", {"query": query[:1000], "kind": "technical"})
        if request.forced_tool == "inspect_image":
            schema = request.tools[0]["function"]["parameters"]
            material_id = schema["properties"]["material_id"]["enum"][0]
            return call(
                "inspect_image",
                {
                    "material_id": material_id,
                    "purpose": "识别产品型号、故障码及可见异常，标明不确定项。",
                },
            )
        if request.forced_tool == "lookup_purchase":
            return call("lookup_purchase", {})
        if request.forced_tool == "check_warranty":
            return call("check_warranty", {})
        if request.forced_tool == "propose_escalation":
            reason = "用户明确要求人工接续。"
            if task.get("service_signals", {}).get("safety_risk"):
                reason = "用户描述可能的安全异常，已停止常规排障，请人工优先处理。"
            return call(
                "propose_escalation",
                {"reason": reason, "evidence_refs": task.get("evidence_refs", [])[:32]},
            )
        if request.forced_tool == "present_troubleshooting_step":
            schema = request.tools[0]["function"]["parameters"]
            step_ids = schema["properties"]["step_id"].get("enum", [])
            if not step_ids:
                raise ModelError("present_troubleshooting_step_required")
            return call("present_troubleshooting_step", {"step_id": step_ids[0]})
        recent_results: list[dict[str, Any]] = []
        for message in request.messages:
            if message.get("role") == "user":
                # A new user message may supply the missing product clue. Previous
                # turns' failed lookups must not suppress a fresh lookup.
                recent_results = []
            if message.get("role") == "tool":
                try:
                    value = json.loads(message["content"])
                    if isinstance(value, dict):
                        recent_results.append(value)
                except (ValueError, TypeError):
                    pass
        last = recent_results[-1] if recent_results else {}
        if not task.get("product"):
            if query.strip().lower().strip("!！。.?？ ") in {"你好", "您好", "嗨", "hello", "hi"}:
                return say("你好！请告诉我产品品牌、型号，以及遇到的问题，我来帮你确认下一步。")
            if any(word in query for word in ("衣服", "服装", "裤子", "裙子", "鞋子")) and any(
                word in query for word in ("退货", "退款", "退换")
            ):
                return say(
                    "衣服退货需要联系购买平台或原商家，在订单详情中查看退货入口和商家要求。"
                    "这里是安克产品售后演示，无法查询服饰订单或代为办理退款。"
                )
            # Resolve once, then ask for a useful clue. Repeating the same empty
            # lookup used to consume every decision without showing any reply.
            if last.get("status") in {"blocked", "not_found", "needs_info"} or (
                last.get("data", {}).get("candidates") == []
            ):
                return say(
                    "还没有识别出具体产品。请补充品牌、型号或产品标签照片，并描述遇到的现象。"
                    "如果要体验完整演示，可以输入“演示清洁器不工作”。"
                )
            if last.get("status") == "error":
                return say("产品信息暂时读取失败，请稍后重试；你也可以先补充品牌、型号和问题现象。")
            return call("resolve_product", {"query": query[:500]})
        if task.get("service_intent") == "warranty" and task.get("warranty"):
            warranty = task["warranty"]
            if warranty.get("eligibility") == "applicable":
                return say(
                    "已根据模拟购买记录和适用政策核对，购买日期在保修期限内。下方可以查看购买日、截止日和依据；实际受理仍需品牌核验。"
                )
            if warranty.get("eligibility") == "inapplicable":
                return say(
                    "模拟记录显示已超出当前政策的期限。你仍可以请求人工确认维修等服务选项，这不代表无法继续获得帮助。"
                )
            return say(
                "目前的信息还不足以判断质保资格，查不到直营订单不等于没有质保。请按下方清单补充购买凭证和经销商信息，也可以选择人工接续。"
            )
        if last.get("status") in {"blocked", "error"}:
            missing = last.get("data", {}).get("missing_fields", [])
            return say(
                "需要补充信息后继续："
                + (
                    "、".join(missing)
                    if missing
                    else "当前资料或条件不足，可补充产品信息或联系人工。"
                )
            )
        current_step = task.get("current_step") or {}
        feedback = current_step.get("feedback") or current_step.get("user_feedback")
        if isinstance(feedback, dict):
            feedback = feedback.get("feedback") or feedback.get("status")
        if feedback and feedback not in {"recovered", "resolved"}:
            return call(
                "propose_escalation",
                {
                    "reason": "用户反馈排障未解决或无法执行，请人工继续处理。",
                    "evidence_refs": task.get("evidence_refs", [])[:32],
                },
            )
        if current_step and not feedback and (task.get("current_card") or {}).get("type") == "step":
            return say(
                "当前操作卡仍有效，请在卡片选择操作结果；如果无法执行，可选择“无法执行”继续接续。"
            )
        steps = task.get("retrieved_steps", {})
        for step_id, step in steps.items():
            if step.get("product_version") == task.get("product_version", 0) and not task.get(
                "step_history"
            ):
                return call(
                    "present_troubleshooting_step",
                    {
                        "step_id": step_id,
                        "precondition_evidence": task.get("evidence_refs", [])[:32],
                    },
                )
        if last.get("data", {}).get("evidence") is not None and not steps:
            return say(
                "已找到当前可用资料；尚无满足条件的审核操作步骤，请补充故障细节或请求人工接续。"
            )
        return call(
            "search_knowledge", {"query": task.get("goal", query)[:1000], "kind": "technical"}
        )
