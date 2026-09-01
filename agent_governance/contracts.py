"""Versioned, serialisable contracts shared by the new Agent domains."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class GateContext:
    """Minimal data that a gate is allowed to inspect.

    Deliberately do not put a document body, Tavily credential, or raw webpage
    into this contract.  ``input_summary_hash`` is enough to correlate audit
    records without exposing user data.
    """

    request_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None
    requested_domain: str
    action: str
    mode_lock: str = "AUTO"
    workflow_id: str | None = None
    trace_id: str | None = None
    feature_flags: dict[str, bool] = field(default_factory=dict)
    input_summary_hash: str = ""
    attachment_ids: tuple[str, ...] = ()
    risk_level: str = "READ_ONLY"
    data_classification: str = "PUBLIC"
    normalized_query: str | None = None
    research_brief_id: str | None = None
    policy_version: str = "p0-2026-08-18"

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["attachment_ids"] = list(self.attachment_ids)
        return payload


@dataclass(frozen=True)
class GateDecision:
    gate: str
    decision: str
    reason_code: str
    allowed_scope: dict[str, Any] = field(default_factory=dict)
    expiration_at: str | None = None
    idempotency_key: str | None = None
    policy_version: str = "p0-2026-08-18"
    audit_event_id: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowEnvelope:
    workflow_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None
    kind: str
    status: str
    input_hash: str
    output_hash: str | None = None
    research_run_id: str | None = None
    office_job_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class GateError(Exception):
    """Stable policy failure, safe to map to an API response."""

    def __init__(self, decision: GateDecision, message: str | None = None):
        self.decision = decision
        self.message = message or decision.reason_code
        super().__init__(self.message)


def expires_at_iso(now: datetime, seconds: int) -> str:
    return now.replace(microsecond=0).isoformat() if seconds <= 0 else (
        now.fromtimestamp(now.timestamp() + seconds, tz=now.tzinfo).replace(microsecond=0).isoformat()
    )
