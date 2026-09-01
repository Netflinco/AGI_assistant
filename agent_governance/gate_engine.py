"""A fail-closed, persistable G0-G7 gate executor."""

from __future__ import annotations

import uuid
from typing import Any, Callable

from .audit import audit_payload
from .contracts import GateContext, GateDecision, GateError
from .policy_registry import POLICY_VERSION


class GateEngine:
    """Run ordered policy checks and persist every decision.

    ``check`` callbacks must be pure: no network, asset parsing, model calls or
    worker submission.  The caller runs side effects only after all decisions
    are ``ALLOW``.  Workers can replay the same checks with the stored context.
    """

    ORDER = ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7")

    def __init__(self, conn: Any, *, now: str, audit_logger: Callable[..., Any] | None = None):
        self.conn = conn
        self.now = now
        self.audit_logger = audit_logger

    def record(self, context: GateContext, decision: GateDecision) -> GateDecision:
        decision_id = decision.audit_event_id or f"gate_{uuid.uuid4().hex[:16]}"
        recorded = GateDecision(
            gate=decision.gate,
            decision=decision.decision,
            reason_code=decision.reason_code,
            allowed_scope=decision.allowed_scope,
            expiration_at=decision.expiration_at,
            idempotency_key=decision.idempotency_key,
            policy_version=decision.policy_version or POLICY_VERSION,
            audit_event_id=decision_id,
        )
        self.conn.execute(
            """INSERT INTO agent_gate_decisions(
                 decision_id, request_id, workflow_id, tenant_id, user_id,
                 domain, action, gate, decision, reason_code, allowed_scope_json,
                 input_summary_hash, policy_version, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, context.request_id, context.workflow_id, context.tenant_id,
                context.user_id, context.requested_domain, context.action,
                recorded.gate, recorded.decision, recorded.reason_code,
                __import__("json").dumps(recorded.allowed_scope, ensure_ascii=False, separators=(",", ":")),
                context.input_summary_hash, recorded.policy_version, self.now,
            ),
        )
        if self.audit_logger:
            self.audit_logger(
                action="agent.gate.decision",
                object_type="agent_gate_decision",
                object_id=decision_id,
                after=audit_payload(
                    gate=recorded.gate, decision=recorded.decision,
                    reason_code=recorded.reason_code, domain=context.requested_domain,
                    workflow_id=context.workflow_id, input_summary_hash=context.input_summary_hash,
                ),
            )
        return recorded

    def evaluate(self, context: GateContext, checks: list[tuple[str, Callable[[], GateDecision]]]) -> list[GateDecision]:
        decisions: list[GateDecision] = []
        expected_indexes = [self.ORDER.index(gate) for gate, _check in checks]
        if expected_indexes != sorted(expected_indexes):
            raise ValueError("GATE_ORDER_INVALID")
        for gate, check in checks:
            result = check()
            if result.gate != gate:
                raise ValueError("GATE_NAME_MISMATCH")
            recorded = self.record(context, result)
            decisions.append(recorded)
            if not recorded.allowed:
                raise GateError(recorded)
        return decisions
