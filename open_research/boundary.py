"""G2R egress policy: public queries only, no direct URL fetches."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from agent_governance.contracts import GateDecision


_SECRET = re.compile(r"(?i)(?:api[_-]?key|app[_-]?secret|token|password|authorization|bearer)\s*[:= ]\s*[\w.-]{8,}")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CN_ID = re.compile(r"\b\d{17}[\dXx]\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_BUSINESS = ("门店", "店铺", "摄像头", "告警", "客户", "内部项目", "经营指标", "销售额", "毛利", "deepvision", "租户")


def classify_query(query: str) -> str:
    value = str(query or "")
    if _SECRET.search(value):
        return "SECRET"
    if _PHONE.search(value) or _EMAIL.search(value) or _CN_ID.search(value) or _CARD.search(value):
        return "PII"
    if any(term.lower() in value.lower() for term in _BUSINESS):
        return "ENTERPRISE"
    if re.search(r"(?i)(?:file|ftp|smb)://", value):
        return "DIRECT_URL"
    for raw in re.findall(r"https?://[^\s]+", value):
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".local", ".internal")):
            return "DIRECT_URL"
    return "PUBLIC"


def egress_decision(query: str) -> GateDecision:
    classification = classify_query(query)
    if classification == "PUBLIC":
        return GateDecision("G2", "ALLOW", "RESEARCH_EGRESS_ALLOWED", {"classification": "PUBLIC", "provider": "tavily"})
    if classification == "DIRECT_URL":
        return GateDecision("G2", "BLOCK", "RESEARCH_DIRECT_FETCH_FORBIDDEN", {"classification": classification})
    return GateDecision("G2", "BLOCK", "RESEARCH_EGRESS_BLOCKED", {"classification": classification})
