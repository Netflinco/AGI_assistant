"""Platform-managed source reputation for evidence-first public research.

The provider never gets to promote a source by itself.  An active policy is a
*positive reputation signal* for a host, however, not an allow-list gate: a
safe, relevant and semantically direct result from an unlisted host remains a
candidate for the evidence synthesiser.  This keeps recall intact while still
letting the platform give official, encyclopaedic or specialist sites an
explicit and auditable confidence prior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib.parse import urlparse


SOURCE_TIERS = {"OFFICIAL", "PRIMARY", "PUBLISHER", "SECONDARY"}
SOURCE_POLICY_STATUSES = {"DRAFT", "ACTIVE", "DISABLED", "EXPIRED"}
DOMAIN_PATTERN = re.compile(r"(?i)^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
DEFAULT_SOURCE_REPUTATION = 0.55
TIER_DEFAULT_REPUTATION = {
    "OFFICIAL": 0.98,
    "PRIMARY": 0.92,
    "PUBLISHER": 0.86,
    "SECONDARY": DEFAULT_SOURCE_REPUTATION,
}


@dataclass(frozen=True)
class SourcePolicy:
    policy_id: str
    domain: str
    match_subdomains: bool
    tier: str
    allowed_fact_types: tuple[str, ...]
    reputation_weight: float
    status: str
    reviewed_by: str | None
    reviewed_at: str | None
    expires_at: str | None

    def supports(self, fact_intent: str) -> bool:
        return "*" in self.allowed_fact_types or fact_intent in self.allowed_fact_types

    def matches(self, host: str) -> bool:
        normalized = str(host or "").lower().rstrip(".")
        return normalized == self.domain or (self.match_subdomains and normalized.endswith(f".{self.domain}"))

    def public_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "domain": self.domain,
            "match_subdomains": self.match_subdomains,
            "tier": self.tier,
            "allowed_fact_types": list(self.allowed_fact_types),
            "reputation_weight": self.reputation_weight,
            "status": self.status,
            "reviewed_at": self.reviewed_at,
            "expires_at": self.expires_at,
        }


def normalize_domain(value: object) -> str:
    domain = str(value or "").strip().lower().rstrip(".")
    if not DOMAIN_PATTERN.fullmatch(domain):
        raise ValueError("invalid source policy domain")
    if domain.endswith((".local", ".internal", ".localhost")):
        raise ValueError("invalid source policy domain")
    return domain


def _parse_fact_types(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    if not isinstance(value, list):
        return ()
    allowed = []
    for item in value:
        intent = str(item or "").strip().upper()
        if intent and intent not in allowed:
            allowed.append(intent)
    return tuple(allowed)


def load_active_source_policies(conn: Any, *, now: datetime | None = None) -> list[SourcePolicy]:
    now = now or datetime.now(timezone.utc)
    timestamp = now.isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT policy_id, domain, match_subdomains, tier, allowed_fact_types_json, reputation_weight,
                  status, reviewed_by, reviewed_at, expires_at
             FROM research_source_policies
             WHERE status='ACTIVE' AND (expires_at IS NULL OR expires_at>?)
             ORDER BY LENGTH(domain) DESC, reviewed_at DESC""",
        (timestamp,),
    ).fetchall()
    return [
        SourcePolicy(
            policy_id=str(row["policy_id"]),
            domain=str(row["domain"]),
            match_subdomains=bool(row["match_subdomains"]),
            tier=str(row["tier"]),
            allowed_fact_types=_parse_fact_types(row["allowed_fact_types_json"]),
            reputation_weight=max(0.0, min(1.0, float(row["reputation_weight"] if row["reputation_weight"] is not None else DEFAULT_SOURCE_REPUTATION))),
            status=str(row["status"]),
            reviewed_by=str(row["reviewed_by"] or "") or None,
            reviewed_at=str(row["reviewed_at"] or "") or None,
            expires_at=str(row["expires_at"] or "") or None,
        )
        for row in rows
    ]


def policy_for_url(url: str, policies: list[SourcePolicy]) -> SourcePolicy | None:
    host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    return next((policy for policy in policies if policy.matches(host)), None)


def reviewed_domains(policies: list[SourcePolicy], *, fact_intent: str) -> tuple[str, ...]:
    """Compatibility helper for older callers.

    Search planning must not restrict recall to this list.  It remains useful
    for diagnostics and for optional ranking, so fact-type filtering is kept
    here without making it a result-admission decision.
    """
    return tuple(
        policy.domain
        for policy in policies
        if policy.tier in {"OFFICIAL", "PRIMARY", "PUBLISHER"} and policy.supports(fact_intent)
    )


def upsert_source_policy(
    conn: Any,
    *,
    policy_id: str,
    domain: object,
    match_subdomains: bool,
    tier: object,
    allowed_fact_types: object,
    status: object,
    reviewed_by: str | None,
    reviewed_at: str | None,
    expires_at: str | None,
    created_by: str,
    now: str,
    reputation_weight: object | None = None,
) -> None:
    normalized_domain = normalize_domain(domain)
    normalized_tier = str(tier or "").upper()
    normalized_status = str(status or "").upper()
    fact_types = _parse_fact_types(allowed_fact_types)
    if not fact_types:
        fact_types = ("*",)
    try:
        weight = float(reputation_weight) if reputation_weight is not None else TIER_DEFAULT_REPUTATION[normalized_tier]
    except (TypeError, ValueError, KeyError):
        raise ValueError("invalid source reputation") from None
    if normalized_tier not in SOURCE_TIERS or normalized_status not in SOURCE_POLICY_STATUSES or not 0.0 <= weight <= 1.0:
        raise ValueError("invalid source policy")
    conn.execute(
        """INSERT INTO research_source_policies(
               policy_id, domain, match_subdomains, tier, allowed_fact_types_json, reputation_weight,
               status, created_by, reviewed_by, reviewed_at, expires_at, created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(domain) DO UPDATE SET
               match_subdomains=excluded.match_subdomains, tier=excluded.tier,
               allowed_fact_types_json=excluded.allowed_fact_types_json, reputation_weight=excluded.reputation_weight, status=excluded.status,
               reviewed_by=excluded.reviewed_by, reviewed_at=excluded.reviewed_at,
               expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
        (
            policy_id,
            normalized_domain,
            1 if match_subdomains else 0,
            normalized_tier,
            json.dumps(list(fact_types), ensure_ascii=False),
            weight,
            normalized_status,
            created_by,
            reviewed_by,
            reviewed_at,
            expires_at,
            now,
            now,
        ),
    )
