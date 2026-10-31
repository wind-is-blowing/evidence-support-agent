"""Auditable service signals, never a diagnosis or a permanent user profile."""

import re


def _positive_match(pattern: str, text: str) -> bool:
    for match in re.finditer(pattern, text, re.IGNORECASE):
        prefix = text[max(0, match.start() - 7) : match.start()]
        if not re.search(r"(?:没有|并未|未见|不是|不需要|不用|无需|不|无)\s*$", prefix):
            return True
    return False


def service_signals(text: str, urgency: str | None = None) -> dict:
    safety = _positive_match(r"冒烟|焦味|烧焦|鼓包|电池膨胀|起火|漏电|触电", text)
    frustrated = _positive_match(r"气死|垃圾|太差|烦死|崩溃|又坏|一直没解决|太失望|投诉", text)
    anxious = bool(urgency) or _positive_match(r"着急|焦虑|怎么办|急死|担心", text)
    intent = "technical"
    if _positive_match(r"转人工|找人工|人工客服|人工接续|人工处理|专员接续", text):
        intent = "handoff"
    elif _positive_match(r"保修|质保|凭证|发票|订单|经销商", text):
        intent = "warranty"
    if safety:
        intent = "handoff"
    emotion = "frustrated" if frustrated else "anxious" if anxious else "neutral"
    acknowledgement = ""
    if safety:
        acknowledgement = (
            "你提到了可能的安全异常，请先停止使用和充电，在确保自身安全的前提下远离设备。"
            "我们先确认型号并整理现象，交给人工处理，不再继续常规排障。"
        )
    elif frustrated:
        acknowledgement = (
            "反复出问题确实让人烦心。我会记下你的情况和已经做过的尝试，陪你把下一步处理清楚。"
        )
    elif anxious:
        acknowledgement = "我知道你现在很着急。我们先确认是哪台设备，再按现象一步步处理；还没解决时，我会把记录一起交给人工。"
    return {
        "intent": intent,
        "emotion": emotion,
        "priority": "safety" if safety else "urgent" if urgency else "normal",
        "safety_risk": safety,
        "acknowledgement": acknowledgement,
        "basis": text[:500],
        "method": "explicit_text_rules_v1",
        "notice": "仅根据本次表达识别服务线索，可随新描述更新。",
    }


def visible_image_text(task: dict) -> str:
    return "\n".join(
        str(text)[:160]
        for item in task.get("image_observations", [])[-3:]
        if item.get("status") == "ok" and item.get("readable")
        for text in item.get("visible_text", [])[:8]
    )[:900]
