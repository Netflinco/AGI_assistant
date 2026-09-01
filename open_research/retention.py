"""Claim-level private-knowledge lifecycle policy.

The default is intentionally conservative: a predicate without an explicit
policy can never become reusable knowledge merely because a source or model
looks confident.
"""

from __future__ import annotations

from .claims import ACTUAL_RELEASE, ResearchClaim
from .planner import FactAssessment


PERMANENT_FACT = "PERMANENT_FACT"
SLOW_60D = "SLOW_60D"
NO_MEMORY = "NO_MEMORY"


def retention_class(assessment: FactAssessment, claims: list[ResearchClaim]) -> str:
    if assessment.fact_intent in {"PRICE_WEATHER_FLIGHT", "LIVE_STATUS"}:
        return NO_MEMORY
    if assessment.fact_intent == "POLICY_APPOINTMENT":
        # A generic evidence bundle is not an office-holder fact.  Future
        # structured appointment extractors must opt in by predicate.
        if claims and all(item.predicate in {"CURRENT_OFFICE_HOLDER", "CURRENT_POLICY_STATUS"} for item in claims):
            return SLOW_60D
        return NO_MEMORY
    if assessment.fact_intent == "EVENT_DATE":
        # A completed historical event in an explicitly identified region is
        # stable.  A future schedule, or a date with no region, is not reusable:
        # the latter may be relevant to a different user target on a later run.
        if claims and all(
            item.predicate == "RELEASE_DATE"
            and item.event_state == "RELEASED"
            and item.date_role == ACTUAL_RELEASE
            and item.territory
            for item in claims
        ):
            return PERMANENT_FACT
        return NO_MEMORY
    # P0.5 has no generic structured predicates yet.  Do not archive evidence
    # sets as a surrogate fact; that would bypass this policy later.
    return NO_MEMORY


def allows_reuse(value: str) -> bool:
    return value in {PERMANENT_FACT, SLOW_60D}
