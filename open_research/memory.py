"""User-private, TTL-bound metadata-only research memory helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from .retention import PERMANENT_FACT, SLOW_60D, allows_reuse

MEMORY_RETENTION_DAYS = 60


def active_memories(conn: Any, *, tenant_id: str, user_id: str, topic: str, now: datetime) -> list[dict]:
    # `expires_at` is calculated when the memory is archived.  Comparing it
    # against a second, moving 60-day cutoff accidentally retains a record for
    # up to 120 days; it must be compared with the request time itself.
    current_time = now.isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT * FROM open_research_memory_index
           WHERE tenant_id=? AND user_id=? AND topic=? AND status='ACTIVE'
             AND retention_class IN (?, ?) AND expires_at>?
           ORDER BY updated_at DESC LIMIT 5""",
        (tenant_id, user_id, topic[:180], PERMANENT_FACT, SLOW_60D, current_time),
    ).fetchall()
    return [dict(row) for row in rows]


def archive_memory(conn: Any, *, memory_id: str, tenant_id: str, user_id: str, topic: str,
                   value: dict, now: datetime) -> None:
    lifecycle = str(value.get("retention_class") or "")
    if not allows_reuse(lifecycle):
        return
    expires = (
        "9999-12-31T23:59:59+00:00" if lifecycle == PERMANENT_FACT
        else (now + timedelta(days=MEMORY_RETENTION_DAYS)).isoformat(timespec="seconds")
    )
    # Store facts and source identifiers only.  No webpage text/snippets or
    # provider response bodies may cross the 60-day user-private boundary.
    allowed = {
        "aliases": value.get("aliases", []), "status": value.get("status"),
        "last_verified_at": value.get("last_verified_at"), "official_domain": value.get("official_domain"),
        "evidence_ids": value.get("evidence_ids", []),
        "fact_intent": value.get("fact_intent"),
        "claims": value.get("claims", []),
        "retention_class": lifecycle,
    }
    conn.execute(
        """INSERT INTO open_research_memory_index(
             memory_id, tenant_id, user_id, topic, memory_json, retention_class, status, created_at, updated_at, expires_at, deleted_at)
           VALUES(?,?,?,?,?,?,'ACTIVE',?,?,?,NULL)""",
        (memory_id, tenant_id, user_id, topic[:180], json.dumps(allowed, ensure_ascii=False, separators=(",", ":")),
         lifecycle, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), expires),
    )


def delete_memory(conn: Any, *, memory_id: str, tenant_id: str, user_id: str, now: datetime) -> bool:
    result = conn.execute(
        """UPDATE open_research_memory_index SET status='DELETED', deleted_at=?, updated_at=?
           WHERE memory_id=? AND tenant_id=? AND user_id=? AND status='ACTIVE'""",
        (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), memory_id, tenant_id, user_id),
    )
    return result.rowcount > 0
