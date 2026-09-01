"""Fail-closed runtime quotas shared by the new domains.

The counters contain only request hashes and scope identifiers.  They are not
an analytics store for raw queries or document content.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
from typing import Any

from .audit import summary_hash
from .contracts import GateDecision


DEFAULT_RESEARCH_WINDOW_SECONDS = 60
DEFAULT_RESEARCH_REQUESTS = 20


def research_quota_decision(conn: Any, *, tenant_id: str, user_id: str, now: datetime) -> GateDecision:
    window_seconds, max_requests = research_limit(conn, tenant_id=tenant_id)
    since = (now - timedelta(seconds=window_seconds)).isoformat(timespec="seconds")
    row = conn.execute(
        """SELECT COUNT(*) AS count FROM agent_runtime_usage
           WHERE tenant_id=? AND user_id=? AND domain='OPEN_RESEARCH' AND created_at>?""",
        (tenant_id, user_id, since),
    ).fetchone()
    count = int(row["count"] if hasattr(row, "keys") else row[0])
    if count >= max_requests:
        return GateDecision(
            "G5", "BLOCK", "RESEARCH_RATE_LIMITED",
            {"window_seconds": window_seconds, "max_requests": max_requests},
        )
    return GateDecision(
        "G5", "ALLOW", "RESEARCH_RUNTIME_AVAILABLE",
        {"window_seconds": window_seconds, "max_requests": max_requests, "remaining_before_request": max_requests - count},
    )


def reserve_research_request(conn: Any, *, tenant_id: str, user_id: str, request_value: Any, now: datetime) -> None:
    """Reserve after G0-G5 has passed and immediately before Tavily is called."""
    conn.execute(
        """INSERT INTO agent_runtime_usage(usage_id, tenant_id, user_id, domain, request_hash, created_at)
           VALUES(?,?,?,?,?,?)""",
        (
            f"usage_{uuid.uuid4().hex[:16]}", tenant_id, user_id, "OPEN_RESEARCH",
            summary_hash(request_value), now.isoformat(timespec="seconds"),
        ),
    )


def research_limit(conn: Any, *, tenant_id: str) -> tuple[int, int]:
    row = conn.execute(
        """SELECT window_seconds, max_requests FROM agent_runtime_limits
           WHERE tenant_id=? AND domain='OPEN_RESEARCH'""",
        (tenant_id,),
    ).fetchone()
    if not row:
        return DEFAULT_RESEARCH_WINDOW_SECONDS, DEFAULT_RESEARCH_REQUESTS
    window = int(row["window_seconds"] if hasattr(row, "keys") else row[0])
    maximum = int(row["max_requests"] if hasattr(row, "keys") else row[1])
    return max(1, min(window, 86400)), max(1, min(maximum, 10000))


def set_research_limit(conn: Any, *, tenant_id: str, window_seconds: int, max_requests: int, now: datetime | None = None) -> None:
    timestamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO agent_runtime_limits(tenant_id, domain, window_seconds, max_requests, updated_at)
           VALUES(?, 'OPEN_RESEARCH', ?, ?, ?)
           ON CONFLICT(tenant_id, domain) DO UPDATE SET
             window_seconds=excluded.window_seconds, max_requests=excluded.max_requests, updated_at=excluded.updated_at""",
        (tenant_id, max(1, int(window_seconds)), max(1, int(max_requests)), timestamp),
    )
