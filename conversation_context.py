"""Deterministic conversation-continuation contracts.

This module deliberately does not know database rows, tenant credentials or
vendor APIs.  It decides whether a new utterance relates to an already
authorized task and describes *what may be inherited*.  Real organization and
evidence identifiers are resolved and authorized by the server/agent layers.
"""

from __future__ import annotations

import re
from typing import Any


VISUAL_DOMAIN = "VISUAL_INSPECTION"

_RESET_MARKERS = (
    "新问题",
    "重新开始",
    "不要沿用",
    "不用刚才",
    "忽略上一轮",
    "清除上下文",
)

_SAME_FRAME_MARKERS = (
    "这张图",
    "这幅图",
    "刚才那张图",
    "上一张图",
    "同一张图",
    "这帧",
    "刚才的画面",
)

_CURRENT_MARKERS = (
    "现在",
    "目前",
    "当前画面",
    "最新",
    "实时",
    "再看一下",
    "重新看",
    "重新检查",
)

_VISUAL_REFERENCE_MARKERS = (
    "这些",
    "这个画面",
    "上面",
    "刚才",
    "刚刚",
    "图中",
    "画面里",
    "画面中",
    "视频里",
    "视频中",
    "里面",
    "那个",
    "那家",
    "该店",
    "上一家",
)

_CONTINUATION_PREFIX = re.compile(
    r"^(?:再|在帮我|继续|接着|那|那么|还有|然后|换成|换到|改成|改为|"
    r"只看|仅看|其他门店|其它门店|当前门店|页面这家店|上一家|回到刚才|不是)"
)

_ELLIPTICAL_SUFFIX = re.compile(r"(?:呢|吗|么|怎么样|如何|还有没有|还在不在)[？?]?$")

_CROSS_DOMAIN_MARKERS = (
    "天气",
    "新闻",
    "上映",
    "股价",
    "价格走势",
    "旅行",
    "旅游",
    "攻略",
    "翻译",
    "写邮件",
    "发邮件",
    "ppt",
    "幻灯片",
    "word",
    "excel",
    "pdf",
)


def normalize_turn_text(text: str) -> str:
    """Normalize only high-confidence conversational slips.

    The reported input uses ``在帮我`` where ``再帮我`` is intended.  Keep the
    correction narrow so ordinary uses of ``在`` are not rewritten.
    """

    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    normalized = re.sub(r"^在帮我(?=(?:看|找|查|检查|识别|判断))", "再帮我", normalized)
    return normalized


def _scope_operation(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    if any(marker in compact for marker in ("当前门店", "页面这家店", "页面当前店")):
        return "RETURN_PAGE_SCOPE"
    if any(marker in compact for marker in ("上一家", "刚才那家", "回到刚才")):
        return "PREVIOUS_SCOPE"
    if any(marker in compact for marker in ("对比", "比较", "比一下")) and any(
        marker in compact for marker in ("两家", "这些店", "这几家", "它们", "门店")
    ):
        return "COMPARE_SCOPE"
    if any(marker in compact for marker in ("其他门店", "其它门店", "其余门店")):
        return "EXPAND_SCOPE"
    if re.search(r"(?:全部|所有|每家|各个|全量).{0,10}(?:门店|店铺|店)", compact):
        return "EXPAND_SCOPE"
    if re.search(r"^(?:只看|仅看|只查|仅查|只复查)", compact):
        return "NARROW_SCOPE"
    if re.search(r"^(?:换成|换到|改成|改为|再看).{1,40}(?:门店|店铺|店)", compact):
        return "REPLACE_SCOPE"
    return "KEEP_SCOPE"


def _looks_cross_domain(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _CROSS_DOMAIN_MARKERS)


def _effective_visual_query(previous: str, current: str) -> str:
    previous = re.sub(r"\s+", " ", str(previous or "")).strip()[:1000]
    current = re.sub(r"\s+", " ", str(current or "")).strip()[:600]
    if not previous:
        return current
    # Persisted effective queries may themselves be continuation prompts.
    # Flatten them before adding the new patch so a long conversation does not
    # recursively duplicate system instructions or accidentally feed stale
    # range control words back into the router.
    previous = previous.replace("上一轮视觉任务：", "")
    previous = previous.replace("本轮用户补充：", "；")
    previous = previous.replace(
        "请把本轮补充理解为对上一轮对象、属性、关系、数量、位置或范围的修改/追问，"
        "仅回答合并后的视觉问题，不扩展到用户没有询问的事项。",
        "",
    )
    previous = re.sub(r"(?:\s*；\s*)+", "；", previous).strip(" ；")[:1000]
    return (
        f"上一轮视觉任务：{previous}\n"
        f"本轮用户补充：{current}\n"
        "请把本轮补充理解为对上一轮对象、属性、关系、数量、位置或范围的修改/追问，"
        "仅回答合并后的视觉问题，不扩展到用户没有询问的事项。"
    )[:1800]


def decide_continuation(
    text: str,
    active_context: dict[str, Any] | None,
    page_org_id: str | None,
    mode_override: str = "AUTO",
) -> dict[str, Any]:
    """Return a strict, serializable continuation decision.

    This function never authorizes a store or evidence object.  It only relates
    the utterance to an already loaded context owned by the caller.
    """

    normalized = normalize_turn_text(text)
    mode = str(mode_override or "AUTO").upper()
    base = {
        "decision": "NEW_TASK",
        "domain": None,
        "context_id": None,
        "context_version": None,
        "scope_operation": "KEEP_SCOPE",
        "inherit": {"predicate": False, "scope": False, "temporal": False, "output_format": False},
        "effective_query": normalized,
        "evidence_mode": "NONE",
        "confidence": 1.0,
        "reason_code": "NO_ACTIVE_CONTEXT",
        "normalized_text": normalized,
        "page_org_id": str(page_org_id or ""),
    }
    if mode == "OPEN_QA":
        return {**base, "reason_code": "EXPLICIT_OPEN_QA_MODE"}
    if not active_context or str(active_context.get("state") or "") != "ACTIVE":
        return base
    if str(active_context.get("domain") or "") != VISUAL_DOMAIN:
        return {**base, "reason_code": "ACTIVE_DOMAIN_NOT_VISUAL"}
    if any(marker in normalized for marker in _RESET_MARKERS):
        return {**base, "reason_code": "EXPLICIT_CONTEXT_RESET"}
    # Explicitly recognizable Office/public-data requests start a new domain
    # even when they use a conversational prefix such as "再帮我".
    if _looks_cross_domain(normalized):
        return {**base, "reason_code": "EXPLICIT_CROSS_DOMAIN_REQUEST"}

    compact = re.sub(r"\s+", "", normalized)
    operation = _scope_operation(normalized)
    active_scope = active_context.get("task_scope") if isinstance(active_context.get("task_scope"), dict) else {}
    active_org_ids = {str(item) for item in active_scope.get("org_ids") or [] if item}
    # When the page selector and the active task point at different stores,
    # bare deixis cannot safely choose between them.  More explicit phrases
    # ("page/current store" and "that/previous store") are handled above.
    ambiguous_scope_reference = bool(re.search(r"(?:这家|这个店|这里|本店)(?:呢|吗|么|怎么样|如何)?[？?]?$", compact))
    if (
        ambiguous_scope_reference
        and page_org_id
        and active_org_ids
        and str(page_org_id) not in active_org_ids
    ):
        return {
            **base,
            "decision": "CLARIFY",
            "domain": VISUAL_DOMAIN,
            "context_id": active_context.get("context_id"),
            "context_version": active_context.get("version"),
            "scope_operation": "CLARIFY_SCOPE",
            "confidence": 0.99,
            "reason_code": "AMBIGUOUS_PAGE_OR_TASK_SCOPE",
            "active_task_scope": active_scope,
        }
    has_reference = any(marker in compact for marker in _VISUAL_REFERENCE_MARKERS)
    has_same_frame_reference = any(marker in compact for marker in _SAME_FRAME_MARKERS)
    has_prefix = bool(_CONTINUATION_PREFIX.search(compact))
    is_elliptical = len(compact) <= 32 and bool(_ELLIPTICAL_SUFFIX.search(compact))
    is_correction = bool(re.search(r"(?:不是.+是|改成|换成|应该是)", compact))
    is_continuation = has_reference or has_same_frame_reference or has_prefix or is_elliptical or is_correction or operation != "KEEP_SCOPE"
    if not is_continuation:
        return {**base, "reason_code": "NO_CONTINUATION_SIGNAL", "confidence": 0.86}

    if any(marker in compact for marker in _SAME_FRAME_MARKERS):
        evidence_mode = "REUSE_SAME_FRAME"
    elif any(marker in compact for marker in _CURRENT_MARKERS):
        evidence_mode = "REFRESH_SAME_SCOPE"
    elif operation == "KEEP_SCOPE":
        evidence_mode = "REFRESH_SAME_SCOPE"
    else:
        evidence_mode = "RECAPTURE_RESOLVED_SCOPE"
    inherit_scope = operation in {"KEEP_SCOPE", "PREVIOUS_SCOPE", "COMPARE_SCOPE"}
    previous_query = str(active_context.get("effective_query") or "")
    return {
        **base,
        "decision": "CONTINUE",
        "domain": VISUAL_DOMAIN,
        "context_id": active_context.get("context_id"),
        "context_version": active_context.get("version"),
        "scope_operation": operation,
        "inherit": {
            "predicate": True,
            "scope": inherit_scope,
            "temporal": evidence_mode == "REUSE_SAME_FRAME",
            "output_format": True,
        },
        "effective_query": _effective_visual_query(previous_query, normalized),
        "evidence_mode": evidence_mode,
        "confidence": 0.98 if has_reference or has_same_frame_reference or has_prefix or operation != "KEEP_SCOPE" else 0.88,
        "reason_code": "VISUAL_FOLLOW_UP",
        "active_task_scope": active_context.get("task_scope") or {},
        "active_evidence_refs": active_context.get("evidence_refs") or [],
        "scope_history": active_context.get("scope_history") or [],
    }


def public_context_summary(context: dict[str, Any]) -> dict[str, Any]:
    """Return the UI-safe subset of a persisted context revision."""

    page_scope = context.get("page_scope") if isinstance(context.get("page_scope"), dict) else {}
    task_scope = context.get("task_scope") if isinstance(context.get("task_scope"), dict) else {}
    decision = context.get("decision") if isinstance(context.get("decision"), dict) else {}
    return {
        "context_id": context.get("context_id"),
        "version": context.get("version"),
        "domain": context.get("domain"),
        "page_scope": {
            "org_id": page_scope.get("org_id"),
            "org_name": page_scope.get("org_name"),
        },
        "task_scope": {
            "type": task_scope.get("type"),
            "source": task_scope.get("source"),
            "org_ids": list(task_scope.get("org_ids") or [])[:50],
            "org_names": list(task_scope.get("org_names") or [])[:50],
        },
        "scope_operation": decision.get("scope_operation"),
        "evidence_mode": decision.get("evidence_mode"),
        "reason_code": decision.get("reason_code"),
    }
