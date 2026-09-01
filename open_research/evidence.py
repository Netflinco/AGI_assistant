"""Citation normalisation and deterministic evidence-quality decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

from .planner import FactAssessment
from .source_policy import DEFAULT_SOURCE_REPUTATION, SourcePolicy, policy_for_url


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    title: str
    canonical_url: str
    publisher: str
    published_at: str | None
    fetched_at: str
    source_tier: str
    snippet: str
    score: float | None = None
    source_policy_id: str | None = None
    evidence_type: str = "DIRECT_SERP_EVIDENCE"
    detail_fetch_status: str | None = None
    extraction_locator_type: str | None = None
    detail_rejection_reason: str | None = None
    relevance_score: float = 0.0
    freshness_score: float = 0.0
    semantic_score: float = 0.0
    source_reputation: float = DEFAULT_SOURCE_REPUTATION
    evidence_confidence: float = 0.0

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def safe_public_url(raw: str) -> str:
    try:
        parsed = urlparse(str(raw or "").strip())
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_reserved or address.is_link_local):
        return ""
    return urlunparse(parsed._replace(query="", fragment="", params=""))


def source_tier(url: str, declared: str | None = None, *, source_policies: list[SourcePolicy] | None = None) -> str:
    """Return a display tier from an optional reputation whitelist.

    ``declared`` is deliberately ignored.  Tavily titles/snippets and any
    provider-provided metadata are untrusted input and must never promote a
    source.  A missing entry is ``SECONDARY`` but remains eligible for content
    evaluation.
    """
    del declared
    policy = policy_for_url(url, source_policies or [])
    return policy.tier if policy else "SECONDARY"


def normalize_citations(
    citations: list[dict[str, Any]],
    *,
    fetched_at: str,
    source_policies: list[SourcePolicy] | None = None,
) -> list[Evidence]:
    output: list[Evidence] = []
    for item in citations[:8]:
        url = safe_public_url(str(item.get("url") or item.get("canonical_url") or ""))
        if not url:
            continue
        snippet = re.sub(r"\s+", " ", str(item.get("snippet") or item.get("content") or "")).strip()[:600]
        # Search snippets are untrusted data.  Drop obvious prompt-injection
        # attempts rather than letting them influence planning or permissions.
        if re.search(r"(?i)(ignore (?:all |previous )?instructions|发送附件|system prompt|工具权限)", snippet):
            continue
        evidence_id = f"ev_{__import__('hashlib').sha256(url.encode('utf-8')).hexdigest()[:16]}"
        policy = policy_for_url(url, source_policies or [])
        output.append(Evidence(
            evidence_id=evidence_id,
            title=re.sub(r"\s+", " ", str(item.get("title") or "未命名来源")).strip()[:180],
            canonical_url=url,
            publisher=re.sub(r"\s+", " ", str(item.get("publisher") or urlparse(url).hostname or "")).strip()[:160],
            published_at=str(item.get("published_at") or item.get("published_date") or "").strip() or None,
            fetched_at=fetched_at,
            source_tier=policy.tier if policy else "SECONDARY",
            snippet=snippet,
            score=float(item["score"]) if item.get("score") is not None else None,
            source_policy_id=policy.policy_id if policy else None,
            source_reputation=policy.reputation_weight if policy else DEFAULT_SOURCE_REPUTATION,
        ))
    return output


def relevant_to_query(evidence: Evidence, query: str) -> bool:
    """Do not let a credible but unrelated page support an entity claim.

    The strict check is applied when a query carries a quoted work/entity title;
    generic public-fact questions remain subject to source and freshness gates.
    """
    match = re.search(r"《([^》]+)》", str(query or ""))
    if not match:
        return True
    entity = match.group(1).strip().lower()
    # A URL slug or publisher mention is discovery metadata, not factual
    # support.  Claim extraction imposes an even stricter same-field/sentence
    # rule, but this keeps clearly unrelated result cards out before details.
    haystack = " ".join((evidence.title, evidence.snippet)).lower()
    return bool(entity and entity in haystack)


def query_relevance_score(evidence: Evidence, query: str) -> float:
    """Score whether a result talks about the requested entity/fact.

    A quoted entity remains an exact hard boundary.  For ordinary public
    questions we intentionally use a soft lexical score: search engines may
    paraphrase a city, office or product name, and a low score is later
    combined with freshness and semantic directness instead of becoming an
    accidental domain-based rejection.
    """
    haystack = f"{evidence.title} {evidence.snippet}".lower()
    quoted = re.search(r"《([^》]+)》", str(query or ""))
    if quoted:
        return 1.0 if quoted.group(1).strip().lower() in haystack else 0.0
    normalized = re.sub(r"[？?。！!，,、：:；;（）()\s]", "", str(query or "").lower())
    normalized = re.sub(r"(?:什么时候|什么时间|啥时候|几时|何时|哪天|多少|怎么样|是什么|是谁|请问|最新|当前|现在|今天|今日|是否|已经|正在|吗)", "", normalized)
    if len(normalized) < 2:
        return 0.45
    if normalized in haystack:
        return 1.0
    # Public-fact questions often carry an action tail (for example “查最新
    # 政策并做 PPT”).  A fact-domain anchor is a meaningful relevance signal,
    # but deliberately not a proof: semantic directness and model citation
    # binding still decide whether the card can support a final conclusion.
    anchors = re.findall(r"政策|法规|条例|天气|航班|股价|价格|汇率|营业|上映|发布|开幕|总统|总理|首相|部长|市长", normalized)
    anchor_match = any(token in haystack for token in anchors)
    bigrams = {normalized[index:index + 2] for index in range(len(normalized) - 1)}
    if not bigrams:
        return 0.45
    matched = sum(1 for token in bigrams if token in haystack)
    lexical = max(0.0, min(0.95, 0.20 + 0.80 * matched / len(bigrams)))
    return max(lexical, 0.70 if anchor_match else 0.0)


def freshness_score(evidence: Evidence, *, assessment: FactAssessment, now: datetime) -> float:
    """Return an intent-sensitive timeliness score without mis-aging stable facts."""
    if assessment.fact_intent in {"EVENT_DATE", "EVERGREEN_FACT"}:
        return 1.0
    timestamp = evidence.published_at or evidence.fetched_at
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)
    hours = max(0.0, age.total_seconds() / 3600)
    if assessment.fact_intent in {"LIVE_STATUS", "PRICE_WEATHER_FLIGHT"}:
        return 1.0 if hours <= 24 else 0.55 if hours <= 48 else 0.0
    if assessment.fact_intent == "POLICY_APPOINTMENT":
        return 1.0 if hours <= 24 * 60 else 0.50 if hours <= 24 * 90 else 0.0
    return 1.0


def semantic_score(evidence: Evidence, *, assessment: FactAssessment, relevance: float) -> float:
    """Score directness of the requested fact, independently of the host."""
    if relevance <= 0.0:
        return 0.0
    content = f"{evidence.title}。{evidence.snippet}"
    if assessment.fact_intent == "EVENT_DATE":
        has_predicate = bool(re.search(r"上映|定档|公映|发行|发布|上线|开幕|开演", content))
        has_date = bool(re.search(r"(?:20\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\b20\d{2}[./-]\d{1,2}[./-]\d{1,2}\b", content))
        return 1.0 if has_predicate and has_date else 0.58 if has_predicate else 0.18
    if assessment.fact_intent == "PRICE_WEATHER_FLIGHT":
        return 0.90 if re.search(r"\d|℃|元|航班|延误|起飞|降水|晴|雨|雪", content) else 0.55
    if assessment.fact_intent == "LIVE_STATUS":
        return 0.88 if re.search(r"当前|现在|今日|状态|营业|开放|进行|停运|在线", content) else 0.55
    if assessment.fact_intent == "POLICY_APPOINTMENT":
        return 0.88 if re.search(r"现任|当前|任命|发布|规定|是|为", content) else 0.55
    return 0.80 if relevance >= 0.55 else 0.45


def assess_evidences(
    evidences: list[Evidence], *, query: str, assessment: FactAssessment, now: datetime,
) -> list[Evidence]:
    """Attach explainable, content-first confidence signals to safe evidence."""
    assessed: list[Evidence] = []
    for item in evidences:
        relevance = query_relevance_score(item, query)
        fresh = freshness_score(item, assessment=assessment, now=now)
        semantic = semantic_score(item, assessment=assessment, relevance=relevance)
        # Content accounts for 60%; the source whitelist is an auditable prior
        # worth 40%.  A domain cannot rescue irrelevant/stale evidence, while a
        # good unlisted result remains usable with an appropriately lower score.
        content_score = 0.45 * relevance + 0.35 * semantic + 0.20 * fresh
        confidence = max(0.0, min(1.0, 0.60 * content_score + 0.40 * item.source_reputation))
        assessed.append(replace(
            item,
            relevance_score=round(relevance, 3),
            freshness_score=round(fresh, 3),
            semantic_score=round(semantic, 3),
            evidence_confidence=round(confidence, 3),
        ))
    return assessed


def evidence_is_fresh(
    evidence: Evidence,
    *,
    now: datetime,
    fact_intent: str | None = None,
    dynamic: bool | None = None,
) -> bool:
    """Apply freshness by fact type; stable event dates are not news items."""
    if fact_intent in {"EVENT_DATE", "EVERGREEN_FACT"}:
        return True
    if fact_intent == "LIVE_STATUS":
        max_age = timedelta(hours=24)
    elif fact_intent == "PRICE_WEATHER_FLIGHT":
        max_age = timedelta(hours=24)
    elif fact_intent == "POLICY_APPOINTMENT":
        max_age = timedelta(days=60)
    elif not dynamic:
        return True
    else:
        # Compatibility for old callers.  New code always passes fact_intent.
        max_age = timedelta(days=7)
    timestamp = evidence.published_at or evidence.fetched_at
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= now.astimezone(timezone.utc) - max_age


def synthesize_status(
    evidences: list[Evidence],
    *,
    now: datetime,
    dynamic: bool | None = None,
    fact_intent: str | None = None,
) -> str:
    """Legacy generic quality check used when no model is configured.

    It intentionally keeps source reputation out of the admission decision;
    source weighting happens in ``assess_evidences`` on the primary path.
    """
    eligible = [
        item for item in evidences
        if evidence_is_fresh(item, dynamic=dynamic, fact_intent=fact_intent, now=now)
    ]
    if not eligible:
        return "NO_AUTHORITATIVE_SOURCE"
    # Tests or a later reviewer may explicitly mark a source as contradictory.
    if any("冲突" in item.snippet or "conflict" in item.snippet.lower() for item in eligible):
        return "CONFLICTING"
    return "VERIFIED"
