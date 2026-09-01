"""Privacy-preserving helpers for new-domain audit payloads."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|app[_-]?secret|token|password)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
)


def summary_hash(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def redact_text(value: str, limit: int = 160) -> str:
    result = str(value or "")
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result[:limit]


def audit_payload(**values: Any) -> dict[str, Any]:
    """Produce a small, safe payload; raw query/body fields are never copied."""
    clean: dict[str, Any] = {}
    for key, value in values.items():
        lowered = key.lower()
        if any(token in lowered for token in ("query", "content", "body", "secret", "token", "credential", "document", "reason", "feedback")):
            clean[f"{key}_hash"] = summary_hash(value)
        elif isinstance(value, str):
            clean[key] = redact_text(value)
        else:
            clean[key] = value
    return clean
