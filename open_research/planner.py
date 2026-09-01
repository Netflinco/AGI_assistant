"""Bounded public-search planning driven by a fact contract, not keywords."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

from .intent import EntityRewrite


FACT_INTENTS = {
    "EVENT_DATE",
    "LIVE_STATUS",
    "PRICE_WEATHER_FLIGHT",
    "POLICY_APPOINTMENT",
    "EVERGREEN_FACT",
}

TERRITORIES = (
    ("中国大陆", "CN-MAINLAND", "中国大陆"),
    ("中国内地", "CN-MAINLAND", "中国大陆"),
    ("内地", "CN-MAINLAND", "中国大陆"),
    ("全国", "CN-MAINLAND", "中国大陆"),
    ("中国香港", "CN-HK", "中国香港"),
    ("香港", "CN-HK", "中国香港"),
    ("中国台湾", "CN-TW", "中国台湾"),
    ("台湾", "CN-TW", "中国台湾"),
    ("北美", "NORTH-AMERICA", "北美"),
    ("美国", "US", "美国"),
    ("日本", "JP", "日本"),
    ("英国", "GB", "英国"),
)


@dataclass(frozen=True)
class FactAssessment:
    fact_intent: str
    dynamic: bool
    territory: str | None = None
    territory_label: str | None = None
    territory_assumed: bool = False

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchQuery:
    query: str
    purpose: str
    freshness: str
    topic: str
    stop_after: int
    include_domains: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["include_domains"] = list(self.include_domains)
        return payload


def territory_for_text(text: str) -> tuple[str | None, str | None]:
    normalized = str(text or "")
    for token, code, label in TERRITORIES:
        if token in normalized:
            return code, label
    return None, None


def territory_label(code: str | None) -> str | None:
    return next((label for _token, item_code, label in TERRITORIES if item_code == code), None)


def classify_fact_intent(text: str) -> FactAssessment:
    """Return the retrieval/freshness contract before constructing a query.

    A release *date* is a stable event fact once it happened.  A question such
    as "现在是否上映" is instead a live status.  Keeping this distinction here
    prevents a keyword like "上映" from silently imposing a one-day search
    window on historical facts.
    """
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    requested_territory, requested_label = territory_for_text(normalized)
    has_event_date = bool(re.search(r"(?:什么时候|什么时间|啥时候|几时|何时|哪天|几月几日|日期|定档|上映时间|发布日|开幕日)", normalized))
    has_event_predicate = bool(re.search(r"上映|发布|开幕|开演|发售|上线", normalized))
    live_cue = bool(re.search(r"最新|当前|现在|今日|今天|实时|是否(?:正在|已经)?上映|有没有上映|仍在", normalized))
    if has_event_predicate and has_event_date and not live_cue:
        return FactAssessment(
            "EVENT_DATE",
            False,
            requested_territory or "CN-MAINLAND",
            requested_label or "中国大陆",
            requested_territory is None,
        )
    if live_cue and (has_event_predicate or re.search(r"停运|开放|营业|状态|比赛", normalized)):
        return FactAssessment("LIVE_STATUS", True, requested_territory, requested_label)
    if re.search(r"价格|股价|汇率|天气|气温|降雨|航班|机票|路况", normalized):
        return FactAssessment("PRICE_WEATHER_FLIGHT", True, requested_territory, requested_label)
    if re.search(r"政策|法规|条例|任命|负责人|现任|通报|总统|总理|首相|部长", normalized):
        # An ordinary office-holder question is slowly changing and may reuse
        # a 60-day private fact.  Explicit present-tense language is a hard
        # refresh signal even for that class.
        return FactAssessment("POLICY_APPOINTMENT", bool(re.search(r"现任|当前|最新|现在|今日|实时", normalized)), requested_territory, requested_label)
    return FactAssessment("EVERGREEN_FACT", False, requested_territory, requested_label)


def event_subject(text: str) -> str:
    """Extract a searchable event subject from quoted and natural-language asks.

    Users commonly omit 《》 (for example ``龙餐馆什么时间上映``).  The
    subject is used only to form a search query; it never resolves an entity or
    proves a fact by itself.
    """
    normalized = re.sub(r"[？?。！!]+$", "", str(text or "")).strip()
    quoted = re.search(r"《([^》]+)》", normalized)
    if quoted:
        return f"《{quoted.group(1).strip()}》"
    subject = re.sub(
        r"(?:什么时候|什么时间|啥时候|几时|何时|哪天|几月几日|日期|定档|上映时间|发布日|开幕日|上映|发布|开幕|开演|发售|上线).*$",
        "",
        normalized,
    ).strip(" ：:，,。；;？?！!")
    return subject or normalized


def is_dynamic_question(text: str) -> bool:
    """Backward-compatible helper for callers outside the fact-quality layer."""
    return classify_fact_intent(text).dynamic


def build_plan(
    question: str,
    rewrite: EntityRewrite,
    *,
    assessment: FactAssessment | None = None,
    memory_hint: dict | None = None,
    reviewed_domains: tuple[str, ...] = (),
) -> list[ResearchQuery]:
    """Return at most three provider-safe query objects with explicit intent."""
    del question  # planning uses the resolved, egress-reviewed public query.
    assessment = assessment or classify_fact_intent(rewrite.rewritten_query)
    base = re.sub(r"[？?。！!]+$", "", rewrite.rewritten_query).strip()
    if assessment.fact_intent == "EVENT_DATE":
        title = event_subject(base)
        territory = assessment.territory_label or "中国大陆"
        queries = [
            ResearchQuery(f"{title} {territory} 上映 日期", "find_event_date_in_target_territory", "general", "general", 1),
            ResearchQuery(f"{title} 定档 上映 官方", "find_event_date_authoritative_source", "general", "general", 1),
        ]
        # The source catalogue is a ranking/weight configuration, not a search
        # allow-list.  Keep the third wording independent of known domains so
        # new but relevant public evidence remains discoverable.
        queries.append(ResearchQuery(
            f"{title} {territory} 正式上映 媒体报道", "cross_check_event_date_evidence", "general", "general", 1,
        ))
        return queries[:3]

    freshness = "day" if assessment.dynamic else "general"
    purpose = {
        "LIVE_STATUS": "verify_live_status",
        "PRICE_WEATHER_FLIGHT": "verify_time_sensitive_public_fact",
        "POLICY_APPOINTMENT": "verify_policy_or_appointment",
    }.get(assessment.fact_intent, "validate_primary_claim")
    queries = [ResearchQuery(base, purpose, freshness, "news" if assessment.dynamic else "general", 1)]
    if memory_hint and memory_hint.get("official_domain"):
        queries.append(ResearchQuery(
            f"{base} site:{memory_hint['official_domain']}",
            "refresh_known_official_source",
            freshness,
            "general",
            1,
            (str(memory_hint["official_domain"]),),
        ))
    return queries[:3]
