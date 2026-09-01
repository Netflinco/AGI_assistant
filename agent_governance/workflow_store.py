"""Persistence helpers for the cross-domain workflow envelope."""

from __future__ import annotations

import uuid
from typing import Any

from .audit import summary_hash


def create_workflow(conn: Any, *, tenant_id: str, user_id: str, conversation_id: str | None,
                    kind: str, input_value: Any, now: str) -> dict[str, Any]:
    workflow_id = f"wf_{uuid.uuid4().hex[:16]}"
    input_hash = summary_hash(input_value)
    conn.execute(
        """INSERT INTO agent_workflow_runs(
             workflow_id, tenant_id, user_id, conversation_id, kind, status,
             input_hash, output_hash, research_run_id, office_job_id, created_at, updated_at)
           VALUES(?,?,?,?,?,'DRAFT',?,NULL,NULL,NULL,?,?)""",
        (workflow_id, tenant_id, user_id, conversation_id, kind, input_hash, now, now),
    )
    return get_workflow(conn, workflow_id, tenant_id, user_id)


def get_workflow(conn: Any, workflow_id: str, tenant_id: str, user_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM agent_workflow_runs WHERE workflow_id=? AND tenant_id=? AND user_id=?",
        (workflow_id, tenant_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def update_workflow(conn: Any, workflow_id: str, *, status: str, now: str,
                    research_run_id: str | None = None, office_job_id: str | None = None,
                    output_value: Any | None = None) -> None:
    fields = ["status=?", "updated_at=?"]
    args: list[Any] = [status, now]
    if research_run_id is not None:
        fields.append("research_run_id=?")
        args.append(research_run_id)
    if office_job_id is not None:
        fields.append("office_job_id=?")
        args.append(office_job_id)
    if output_value is not None:
        fields.append("output_hash=?")
        args.append(summary_hash(output_value))
    args.append(workflow_id)
    conn.execute(f"UPDATE agent_workflow_runs SET {', '.join(fields)} WHERE workflow_id=?", args)
