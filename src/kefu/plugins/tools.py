"""The complete, deliberately closed model tool registry.

Model arguments never contain identity, authorization or physical file paths.
The service port repeats resource and source checks immediately before access.
"""

import hashlib
import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from kefu.runtime.ports import RuntimePorts


def text_field(limit: int = 1000) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": limit}


REFS = {"type": "array", "items": text_field(200), "maxItems": 32}
ITEM = {
    "type": "object",
    "properties": {
        "id": text_field(80),
        "title": text_field(300),
        "status": {"type": "string", "enum": ["pending", "in_progress", "done", "cancelled"]},
        "evidence_refs": REFS,
    },
    "required": ["id", "title", "status"],
    "additionalProperties": False,
}


def definition(name: str, description: str, fields: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": fields,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOLS = [
    definition(
        "resolve_product",
        "查找产品候选，等待用户确认，不能自行绑定产品。",
        {"query": text_field(500), "evidence_refs": REFS},
        ["query"],
    ),
    definition(
        "inspect_image",
        "观察本任务图片，记录可见事实和不确定项；不证明所有权。",
        {"material_id": text_field(100), "purpose": text_field(500)},
        ["material_id", "purpose"],
    ),
    definition(
        "search_knowledge",
        "检索当前已确认产品及地区渠道适用的技术或政策证据。",
        {"query": text_field(1000), "kind": {"type": "string", "enum": ["technical", "policy"]}},
        ["query", "kind"],
    ),
    definition(
        "lookup_purchase",
        "查当前用户订单或凭证缺项。order_hint 仅填用户提供的准确订单号或已选购买记录 ID，不能填产品名称；没有订单号时省略。查无订单不等于无质保。",
        {"order_hint": text_field(200), "evidence_refs": REFS},
        [],
    ),
    definition(
        "check_warranty",
        "按经核验购买记录和政策检查质保；未知必须保留。",
        {"purchase_ref": text_field(200), "evidence_refs": REFS},
        [],
    ),
    definition(
        "get_service_history",
        "查询当前任务或当前用户产品的服务历史。",
        {"ticket_id": text_field(200)},
        [],
    ),
    definition(
        "present_troubleshooting_step",
        "呈现已检索、已审核且适用的一步操作，等待用户反馈。",
        {"step_id": text_field(200), "precondition_evidence": REFS, "retest_evidence": REFS},
        ["step_id"],
    ),
    definition(
        "propose_escalation",
        "冻结升级草案等待用户确认，不提交外部工单。",
        {"reason": text_field(1500), "evidence_refs": REFS},
        ["reason"],
    ),
    definition(
        "load_skill",
        "按已安装名称加载共享流程，Skill 不授予权限。",
        {
            "name": {
                "type": "string",
                "enum": [
                    "guided-troubleshooting",
                    "purchase-and-warranty",
                    "escalation-and-followup",
                ],
            }
        },
        ["name"],
    ),
    definition(
        "todo",
        "创建或明确复核计划，版本必须匹配；计划状态不代表业务事实。",
        {
            "mode": {"type": "string", "enum": ["update", "review"]},
            "expected_version": {"type": "integer", "minimum": 0},
            "items": {"type": "array", "items": ITEM, "minItems": 1, "maxItems": 12},
            "reviewed": {"type": "boolean"},
        },
        ["mode", "expected_version"],
    ),
    definition(
        "read_context",
        "限长读取登记在当前任务的虚拟上下文路径。",
        {
            "path": text_field(300),
            "cursor": {"type": ["string", "integer", "null"]},
            "keyword": text_field(200),
        },
        ["path"],
    ),
]
REGISTRY = {item["function"]["name"]: item for item in TOOLS}
SKILLS = {
    "guided-troubleshooting": "产品消歧、证据检索、逐步排障和反馈",
    "purchase-and-warranty": "购买记录、经销商凭证与质保核对",
    "escalation-and-followup": "汇总未解决问题、确认升级与人工接续",
}


def result(
    status: str = "ok",
    data: dict | None = None,
    refs: list | None = None,
    code: str | None = None,
    message: str | None = None,
) -> dict:
    return {
        "status": status,
        "data": data or {},
        "evidence_refs": refs or [],
        "error": {"code": code, "message": message or code} if code else None,
    }


def _validate(value: Any, schema: dict, path: str = "arguments") -> None:
    kinds = schema.get("type", [])
    if isinstance(kinds, str):
        kinds = [kinds]
    checks = {
        "object": lambda x: isinstance(x, dict),
        "array": lambda x: isinstance(x, list),
        "string": lambda x: isinstance(x, str),
        "integer": lambda x: type(x) is int,
        "boolean": lambda x: type(x) is bool,
        "null": lambda x: x is None,
        "number": lambda x: type(x) in {int, float} and math.isfinite(x),
    }
    if kinds and not any(checks[kind](value) for kind in kinds):
        raise ValueError(f"{path}: invalid type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unsupported value")
    if isinstance(value, str) and not schema.get("minLength", 0) <= len(value) <= schema.get(
        "maxLength", 10000
    ):
        raise ValueError(f"{path}: invalid length")
    if type(value) in {int, float} and not schema.get("minimum", value) <= value <= schema.get(
        "maximum", value
    ):
        raise ValueError(f"{path}: outside allowed range")
    if isinstance(value, dict):
        fields = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise ValueError(f"{path}: required field missing")
        if schema.get("additionalProperties") is False and value.keys() - fields.keys():
            raise ValueError(f"{path}: unknown fields are forbidden")
        for key, child in value.items():
            _validate(child, fields.get(key, {}), f"{path}.{key}")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise ValueError(f"{path}: invalid item count")
        for child in value:
            _validate(child, schema.get("items", {}), path + "[]")


def validate_arguments(name: str, arguments: dict) -> None:
    if name not in REGISTRY:
        raise ValueError("unknown tool")
    _validate(arguments, REGISTRY[name]["function"]["parameters"])


def requires_plan(task: dict) -> bool:
    return (
        not (task.get("todo") or {}).get("items")
        or task.get("plan_goal_version") != task.get("goal_version", 0)
        or task.get("plan_product_version") != task.get("product_version", 0)
        or task.get("steps_since_todo", 0) >= 3
    )


class ToolExecutor:
    def __init__(self, skill_root: str | Path | None = None):
        self.skill_root = (
            Path(skill_root) if skill_root else Path(__file__).resolve().parents[3] / "skills"
        )

    def _todo(self, args: dict, task: dict) -> dict:
        plan = task.get("todo") or {"version": 0, "items": []}
        if args["expected_version"] != plan["version"]:
            return result("blocked", code="plan_version_conflict")
        if args["mode"] == "review":
            if not plan["items"] or args.get("reviewed") is not True:
                return result("blocked", code="explicit_plan_review_required")
            if task.get("plan_goal_version") != task.get("goal_version", 0) or task.get(
                "plan_product_version"
            ) != task.get("product_version", 0):
                return result("blocked", code="replan_required")
        else:
            items = args.get("items", [])
            if not items or sum(item["status"] == "in_progress" for item in items) > 1:
                return result("blocked", code="invalid_plan")
            if len({item["id"] for item in items}) != len(items):
                return result("blocked", code="duplicate_plan_ids")
            plan = {**plan, "items": deepcopy(items)}
        plan = {
            **plan,
            "version": plan["version"] + 1,
            "reviews": plan.get("reviews", 0) + int(args["mode"] == "review"),
        }
        task.update(
            todo=plan,
            steps_since_todo=0,
            plan_goal_version=task.get("goal_version", 0),
            plan_product_version=task.get("product_version", 0),
        )
        return result(data=deepcopy(plan))

    def _skill(self, name: str) -> dict:
        if name not in SKILLS:
            return result("error", code="skill_not_installed")
        root = self.skill_root.resolve()
        path = root / name / "SKILL.md"
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root):
            return result("error", code="unsafe_skill_path")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return result("error", code="skill_unavailable")
        return result(
            data={
                "name": name,
                "content": content,
                "version": hashlib.sha256(content.encode()).hexdigest()[:16],
            }
        )

    async def execute(
        self, name: str, args: dict, task: dict, ports: RuntimePorts, only_todo: bool = False
    ) -> dict:
        try:
            validate_arguments(name, args)
        except ValueError as error:
            return result("error", code="invalid_arguments", message=str(error))
        if not await ports.is_current(task["id"], task["version"]):
            return result("blocked", code="stale_task")
        if task.get("stop_requested"):
            return result("blocked", code="task_stopped")
        if only_todo and name != "todo":
            return result("blocked", code="todo_required")
        if name == "todo":
            return self._todo(args, task)
        if name == "load_skill":
            return self._skill(args["name"])
        if name in {
            "search_knowledge",
            "check_warranty",
            "present_troubleshooting_step",
        } and not task.get("product"):
            return result(
                "blocked", {"missing_fields": ["confirmed_product"]}, code="product_required"
            )
        if name == "present_troubleshooting_step":
            if task.get("service_intent") in {"handoff", "warranty"}:
                return result("blocked", code="service_route_does_not_allow_physical_step")
            steps = task.get("retrieved_steps", {})
            step = steps.get(args["step_id"])
            if not step or step.get("product_version") != task.get("product_version", 0):
                return result("blocked", code="step_not_retrieved_for_current_product")
            attempted = [
                s
                for s in task.get("step_history", [])
                if s.get("step_id") == args["step_id"]
                and s.get("product_version") == task.get("product_version", 0)
            ]
            known_refs = set(task.get("evidence_refs", []))
            fresh = set(args.get("retest_evidence", [])) - {
                r for s in attempted for r in s.get("evidence_refs", [])
            }
            if attempted and not (fresh & known_refs):
                return result("blocked", code="step_already_attempted")
        if name == "read_context" and (
            ".." in args["path"].split("/")
            or "\\" in args["path"]
            or re.match(r"^[A-Za-z]:", args["path"])
            or "://" in args["path"]
        ):
            return result("blocked", code="invalid_context_path")
        output = await ports.call_tool(name, deepcopy(args), task)
        if isinstance(output, dict) and output.get("status") in {
            "needs_info",
            "not_found",
            "conflict",
        }:
            output = {
                **output,
                "status": "blocked",
                "data": {**output.get("data", {}), "business_status": output["status"]},
            }
        if not isinstance(output, dict) or output.get("status") not in {
            "ok",
            "blocked",
            "error",
            "unknown",
        }:
            return result("error", code="invalid_tool_result")
        output.setdefault("data", {})
        output.setdefault("evidence_refs", [])
        output.setdefault("error", None)
        if name in {"inspect_image", "lookup_purchase", "check_warranty"}:
            if not await ports.is_current(task["id"], task["version"]):
                return result("blocked", code="stale_task")
            data = output["data"]
            if name == "inspect_image":
                task.setdefault("image_attempts", {})[args["material_id"]] = task["version"]
                task.setdefault("image_observations", []).append(
                    {
                        "material_id": args["material_id"],
                        "status": output["status"],
                        "evidence_kind": "model_inference",
                        **data.get("observation", {}),
                        "uncertainties": data.get("observation", {}).get(
                            "uncertainties",
                            data.get("uncertainties", ["图片暂时无法识别，请补充清晰文字。"]),
                        ),
                    }
                )
            elif name == "lookup_purchase":
                task["purchase_lookup_version"] = task["version"]
                task["purchase_lookup"] = deepcopy(data)
            else:
                task["warranty_check_version"] = task["version"]
                task["warranty"] = {
                    **deepcopy(data),
                    "product_version": task.get("product_version", 0),
                }
        if output["status"] != "ok":
            return output
        # A completed network read can race with new input. Its card must not win.
        if not await ports.is_current(task["id"], task["version"]):
            return result("blocked", code="stale_task")
        refs = output["evidence_refs"]
        task["evidence_refs"] = sorted(set(task.get("evidence_refs", [])) | set(refs))
        data = output["data"]
        if name == "resolve_product" and data.get("candidates"):
            card = {
                "type": "product",
                "id": str(uuid4()),
                "version": task["version"],
                "candidates": data["candidates"],
                "prompt": (
                    "你提到「" + task["urgency"] + "」。请确认下面哪款是你的设备。"
                    if task.get("urgency")
                    else "请确认下面哪款是你的设备。"
                ),
            }
            task.update(current_card=card, state="WAITING", waiting_reason="product_confirmation")
            output["data"]["card"] = deepcopy(card)
        elif name == "search_knowledge":
            evidence = {entry["id"]: entry for entry in task.get("evidence", []) if "id" in entry}
            evidence.update(
                {
                    entry["id"]: deepcopy(entry)
                    for entry in data.get("evidence", [])
                    if "id" in entry
                }
            )
            task["evidence"] = list(evidence.values())
            for step in data.get("steps", []):
                task.setdefault("retrieved_steps", {})[step["step_id"]] = {
                    **step,
                    "product_version": task.get("product_version", 0),
                }
        elif name == "present_troubleshooting_step":
            step = data.get("step", data)
            if step.get("reviewed") is not True or step.get("consumer_safe") is not True:
                return result("blocked", code="unsafe_step")
            product = task["product"]
            product_id = (
                (product.get("id") or product.get("product_id"))
                if isinstance(product, dict)
                else product
            )
            if product_id not in step.get("product_ids", []):
                return result("blocked", code="step_product_mismatch")
            instructions = step.get("instructions") or step.get("instruction")
            if not instructions:
                return result("error", code="step_content_missing")
            card = {
                "type": "step",
                "step_instance_id": str(uuid4()),
                "product_version": task.get("product_version", 0),
                "step_id": args["step_id"],
                "product_id": product_id,
                "device_id": task.get("device_id"),
                "title": step.get("title", args["step_id"]),
                "instructions": instructions,
                "warnings": step.get("warnings", []),
                "prerequisites": step.get("preconditions", []),
                "evidence_refs": refs,
                "expected_feedback": step.get("expected_feedback", "请选择操作结果"),
                "source_version": step.get("version"),
                "feedback": None,
            }
            task.update(
                current_card=card,
                current_step=deepcopy(card),
                state="WAITING",
                waiting_reason="step_feedback",
            )
            task.setdefault("step_history", []).append(deepcopy(card))
            output["data"]["card"] = deepcopy(card)
        elif name == "propose_escalation":
            if data.get("missing_fields"):
                return result("blocked", data, refs, code="escalation_missing_fields")
            draft = {
                "type": "escalation",
                "draft_id": str(uuid4()),
                "draft_version": (task.get("draft") or {}).get("draft_version", 0) + 1,
                "task_version": task["version"],
                "product_version": task.get("product_version", 0),
                "product": deepcopy(task.get("product")),
                "recipient": data.get("recipient", "support"),
                "reason": args["reason"],
                "unresolved_issue": task.get("goal", "")[:2000],
                "materials": deepcopy(data.get("materials", [])),
                "attempted_steps": deepcopy(task.get("step_history", [])),
                "pending_checks": deepcopy(data.get("pending_checks", [])),
                "urgency": task.get("urgency"),
                "service_signals": deepcopy(task.get("service_signals", {})),
                "image_observations": deepcopy(task.get("image_observations", [])),
                "purchase_lookup": deepcopy(task.get("purchase_lookup", {})),
                "warranty": deepcopy(task.get("warranty", {})),
                "evidence_refs": list(refs),
                "missing_fields": [],
                "status": "DRAFT",
                "mode": data.get("mode", "unconfigured"),
            }
            draft["content_hash"] = hashlib.sha256(
                json.dumps(draft, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            task.update(
                draft=draft,
                current_card=deepcopy(draft),
                state="WAITING",
                waiting_reason="escalation_confirmation",
            )
            output["data"]["draft"] = deepcopy(draft)
        return output
