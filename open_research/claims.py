"""Deterministic claim extraction and G6 fact-quality evaluation.

Only a bounded title/snippet supplied by the Tavily adapter is inspected.  No
webpage body is fetched or stored, and a date is never invented by a model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import re
from typing import Iterable

from .evidence import Evidence, evidence_is_fresh
from .planner import FactAssessment, event_subject, territory_for_text, territory_label
from .source_policy import SourcePolicy


_CN_DATE = re.compile(r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_ISO_DATE = re.compile(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b")
_EVENT_DATE_TERMS = re.compile(r"上映|定档|公映|发行|发布|上线|开幕|开演")
_ACCEPTED_TIERS = {"OFFICIAL", "PRIMARY", "PUBLISHER"}

# A date next to a work title is not necessarily that work's release date.
# These roles are deliberately semantic rather than temporal: a schedule that
# has already passed remains a schedule, and must not be upgraded to an
# "actual release" merely because the current date is later.
ACTUAL_RELEASE = "ACTUAL_RELEASE"
SCHEDULED_RELEASE = "SCHEDULED_RELEASE"
SUPERSEDED_SCHEDULE = "SUPERSEDED_SCHEDULE"
ANNOUNCEMENT_DATE = "ANNOUNCEMENT_DATE"
PREMIERE_DATE = "PREMIERE_DATE"
PROGRAM_AIR_DATE = "PROGRAM_AIR_DATE"
PAGE_METADATA_DATE = "PAGE_METADATA_DATE"
UNRELATED_DATE = "UNRELATED_DATE"
_DELIVERABLE_EVENT_DATE_ROLES = {ACTUAL_RELEASE, SCHEDULED_RELEASE}


@dataclass(frozen=True)
class ResearchClaim:
    claim_id: str
    subject: str
    predicate: str
    value: str
    territory: str | None
    territory_label: str | None
    event_state: str
    evidence_ids: tuple[str, ...]
    source_tier: str
    source_policy_id: str | None
    claim_status: str = "CANDIDATE"
    confidence: float = 0.0
    evidence_type: str = "DIRECT_SERP_EVIDENCE"
    extraction_locator_type: str | None = None
    date_role: str | None = None

    def public_dict(self) -> dict:
        payload = asdict(self)
        payload["evidence_ids"] = list(self.evidence_ids)
        return payload


def _entity(query: str) -> str:
    match = re.search(r"《([^》]+)》", str(query or ""))
    return match.group(1).strip() if match else event_subject(query)[:120]


def _year_from_evidence(evidence: Evidence) -> int | None:
    # ``fetched_at`` is retrieval metadata, not evidence about when the event
    # happened.  Prefer a publisher timestamp, then a reviewed URL date.  Only
    # if both are absent the candidate is not deliverable.  Inferring the
    # current year creates a false historical claim when a search result omits
    # its publication date.
    if evidence.published_at:
        try:
            return datetime.fromisoformat(evidence.published_at.replace("Z", "+00:00")).year
        except ValueError:
            pass
    # Publishers commonly encode the date as either /2025/07/... or
    # _202507/... in their URL.  This is source-local metadata; never borrow
    # a year from an unrelated search result.
    url_year = re.search(r"(?:/|_)(20\d{2})(?:(?:[/-])|(?:0[1-9]|1[0-2])(?:\D|$))", evidence.canonical_url)
    if url_year:
        return int(url_year.group(1))
    return None


def _date_candidates(text: str, evidence: Evidence, now: datetime) -> Iterable[tuple[str, int | None, int]]:
    for match in _CN_DATE.finditer(text):
        year = int(match.group(1)) if match.group(1) else _year_from_evidence(evidence)
        yield match.group(0), year, match.start()
    for match in _ISO_DATE.finditer(text):
        yield match.group(0), int(match.group(1)), match.start()


def _normalized_date(raw: str, evidence: Evidence, now: datetime, *, fallback_year: int | None = None) -> str | None:
    cn = _CN_DATE.fullmatch(raw)
    iso = _ISO_DATE.fullmatch(raw)
    try:
        if cn:
            year = cn.group(1) or fallback_year or _year_from_evidence(evidence)
            return date(int(year), int(cn.group(2)), int(cn.group(3))).isoformat() if year else None
        if iso:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
    except ValueError:
        return None
    return None


def _claim_id(evidence_id: str, value: str, territory: str | None, date_role: str = "") -> str:
    digest = hashlib.sha256(f"{evidence_id}|{value}|{territory or ''}|{date_role}".encode("utf-8")).hexdigest()[:16]
    return f"cl_{digest}"


def _policy_allows(evidence: Evidence, fact_intent: str, policies: list[SourcePolicy]) -> bool:
    """Legacy compatibility helper; no longer an evidence-admission gate.

    Active source policies now express an auditable reputation prior.  Callers
    that need the old catalogue view may still use this helper, but G6 bases
    delivery on relevance, timeliness and fact semantics instead.
    """
    if evidence.source_tier not in _ACCEPTED_TIERS or not evidence.source_policy_id:
        return False
    return any(policy.policy_id == evidence.source_policy_id and policy.supports(fact_intent) for policy in policies)


def _sentence_for_date(text: str, start: int, date_text: str) -> tuple[str, int]:
    """Return the containing sentence and the date offset inside it."""
    separators = "。！？；;\n"
    sentence_start = max((text.rfind(token, 0, start) for token in separators), default=-1) + 1
    sentence_end_candidates = [position for token in separators if (position := text.find(token, start + len(date_text))) >= 0]
    sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(text)
    return text[sentence_start:sentence_end], start - sentence_start


def _date_role(text: str, start: int, date_text: str, subject: str) -> str:
    """Classify the *relation* owned by a date in one sentence.

    The previous implementation checked whether an event word occurred within
    forty characters.  That lets ``7月13日，电影《…》官宣提档至7月18日上映``
    create two release claims.  Here the release verb has to be attached to
    the candidate date itself, and announcement/old-schedule/program dates
    receive explicit non-deliverable roles.
    """
    sentence, local_start = _sentence_for_date(text, start, date_text)
    before = sentence[max(0, local_start - 72):local_start]
    after = sentence[local_start + len(date_text): min(len(sentence), local_start + len(date_text) + 96)]
    prefix = before[-24:]
    suffix = after[:24]
    if re.search(r"(?:推荐(?:阅读|文章)?|(?:页面)?更新(?:时间)?|最后更新|发布时间|发布于|截至|截止|编辑于|来源|浏览量)\s*[:：]?\s*$", prefix):
        return PAGE_METADATA_DATE
    if re.match(r"\s*(?:更新|发布于|编辑)", suffix):
        return PAGE_METADATA_DATE
    if re.search(r"(?:今日影评|节目|栏目|播出|直播|第\s*\d+\s*期|CCTV[-－]?\d)", sentence):
        return PROGRAM_AIR_DATE
    if re.search(r"(?:首映礼|首映|点映|路演|观影会)", sentence):
        return PREMIERE_DATE

    # The date at the start of an announcement sentence is the announcement
    # date, even when a later date in the same sentence is the release date.
    has_later_date = bool(_CN_DATE.search(after) or _ISO_DATE.search(after))
    if (re.search(r"(?:官宣|宣布|发布|公布|提档|改档)", after)
            and has_later_date
            and not re.search(r"(?:提档至|改档至|提前至|延期至)\s*$", before)):
        return ANNOUNCEMENT_DATE
    if re.search(r"(?:原定|原计划|此前(?:定档|计划)|原档期|改档前|提档前)\s*(?:于|为|在)?\s*$", before):
        return SUPERSEDED_SCHEDULE

    # The target entity must be the grammatical subject near this date.  A
    # mentioned title after an unrelated date cannot borrow the later event
    # predicate unless the date is immediately followed by that title.
    subject_index = sentence.find(subject)
    if subject_index < 0:
        return UNRELATED_DATE
    if subject_index > local_start + len(date_text) + 64:
        return UNRELATED_DATE

    release_after = bool(re.search(r"(?:在\s*(?:中国大陆|中国内地|内地|全国|中国香港|香港|中国台湾|台湾|北美|美国|日本|英国))?\s*(?:正式|已)?\s*(?:上映|公映|发行|上线|开幕|开演)", after[:48]))
    release_before = bool(re.search(r"(?:上映|公映|发行|上线|开幕|开演)\s*(?:于|在)?\s*$", before))
    # News leads often put the date first: ``7月18日，……《影片》正式上映``.
    # This is valid only when the target title itself is followed by the
    # release predicate in the same sentence; the announcement guard above
    # has already rejected ``7月13日，影片官宣提档至7月18日``.
    subject_end = subject_index + len(subject)
    after_subject = sentence[subject_end: min(len(sentence), subject_end + 36)]
    release_after_subject = bool(re.search(
        r"(?:在\s*(?:中国大陆|中国内地|内地|全国|中国香港|香港|中国台湾|台湾|北美|美国|日本|英国))?\s*(?:正式|已)?\s*(?:上映|公映|发行|上线|开幕|开演)",
        after_subject,
    ))
    if not (release_after or release_before or release_after_subject):
        return UNRELATED_DATE

    if (re.search(r"(?:已于|正式于|已经于)\s*$", before)
            or re.search(r"^\s*(?:在[^，。；;]{0,16})?\s*(?:正式|已)\s*(?:上映|公映|发行|上线|开幕|开演)", after)
            or re.search(r"(?:正式|已)\s*(?:上映|公映|发行|上线|开幕|开演)", after_subject)):
        return ACTUAL_RELEASE
    if re.search(r"(?:将于|拟于|定于|定档(?:于|为|：|:)?|提档至|改档至|提前至|延期至|于)\s*$", before):
        return SCHEDULED_RELEASE
    # Headlines often omit a modal verb: ``《影片》 7月18日全国上映``.
    if subject_index <= local_start and local_start - subject_index <= 64:
        return SCHEDULED_RELEASE
    return UNRELATED_DATE


def _date_is_subject_bound(text: str, start: int, date_text: str, subject: str) -> bool:
    """Require the date relation to belong to the requested entity itself.

    A page may mention the target work in a heading and then describe another
    quoted work in the same summary.  The nearest quoted title around a date
    wins; a target mentioned only in another sentence cannot borrow it.
    """
    sentence, local_start = _sentence_for_date(text, start, date_text)
    if subject not in sentence:
        return False
    titles = list(re.finditer(r"《([^》]+)》", sentence))
    if not titles:
        return True
    nearest = min(titles, key=lambda item: min(abs(local_start - item.start()), abs(local_start - item.end())))
    return nearest.group(1).strip() == subject


def extract_event_date_claims(
    evidences: list[Evidence],
    *,
    query: str,
    policies: list[SourcePolicy],
    now: datetime,
) -> list[ResearchClaim]:
    """Extract evidence-bound release date candidates without model inference."""
    subject = _entity(query)
    output: list[ResearchClaim] = []
    seen: set[tuple[str, str | None, str, str]] = set()
    for evidence in evidences:
        # Preserve strict field boundaries.  The entity, predicate and value
        # must co-occur in one title, one summary sentence or one detail
        # fragment.  In particular, title text can never lend a subject to a
        # date found only in an unrelated Tavily snippet.
        fields = (
            (evidence.title, "TITLE"),
            (evidence.snippet, evidence.extraction_locator_type or "SERP_SNIPPET"),
        )
        for text, locator in fields:
            if not text or subject not in text or not _EVENT_DATE_TERMS.search(text):
                continue
            for raw_date, _year, start in _date_candidates(text, evidence, now):
                if not _date_is_subject_bound(text, start, raw_date, subject):
                    continue
                date_role = _date_role(text, start, raw_date, subject)
                if date_role not in _DELIVERABLE_EVENT_DATE_ROLES:
                    continue
                # Omitted years are resolved only from the evidence carrying
                # this claim (publisher timestamp/URL), never another result.
                value = _normalized_date(raw_date, evidence, now)
                if not value:
                    continue
                sentence, local_start = _sentence_for_date(text, start, raw_date)
                context = sentence[max(0, local_start - 20): min(len(sentence), local_start + len(raw_date) + 40)]
                territory, label = territory_for_text(context)
                key = (value, territory, evidence.evidence_id, date_role)
                if key in seen:
                    continue
                seen.add(key)
                state = "RELEASED" if date_role == ACTUAL_RELEASE else "SCHEDULED"
                # Source registration is a confidence prior, never an
                # admission decision.  Direct entity/date/predicate binding is
                # established above and survives an unlisted-domain result.
                confidence = evidence.evidence_confidence or {
                    "OFFICIAL": 0.98, "PRIMARY": 0.94, "PUBLISHER": 0.86,
                }.get(evidence.source_tier, evidence.source_reputation)
                output.append(ResearchClaim(
                    claim_id=_claim_id(evidence.evidence_id, value, territory, date_role),
                    subject=subject,
                    predicate="RELEASE_DATE",
                    value=value,
                    territory=territory,
                    territory_label=label,
                    event_state=state,
                    evidence_ids=(evidence.evidence_id,),
                    source_tier=evidence.source_tier,
                    source_policy_id=evidence.source_policy_id,
                    claim_status="CANDIDATE",
                    confidence=confidence,
                    evidence_type=evidence.evidence_type,
                    extraction_locator_type=locator,
                    date_role=date_role,
                ))
    return output


def _future_claim_is_fresh(claim: ResearchClaim, evidence_by_id: dict[str, Evidence], now: datetime) -> bool:
    if claim.date_role != SCHEDULED_RELEASE:
        return True
    # A pre-release schedule is not proof that the event subsequently happened.
    # Once its date is in the past, force the pipeline to find an actual-release
    # statement rather than silently upgrading the old schedule.
    if claim.value <= now.date().isoformat():
        return False
    evidence = next((evidence_by_id.get(item) for item in claim.evidence_ids if evidence_by_id.get(item)), None)
    if not evidence:
        return False
    timestamp = evidence.published_at or evidence.fetched_at
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= now.astimezone(timezone.utc) - timedelta(days=30)


def evaluate_event_date_claims(
    claims: list[ResearchClaim],
    *,
    evidences: list[Evidence],
    assessment: FactAssessment,
    policies: list[SourcePolicy],
    now: datetime,
) -> tuple[str, list[ResearchClaim]]:
    """Evaluate event claims by evidence semantics before source reputation.

    Source reputation influences confidence but never rejects an otherwise
    direct, relevant and timely event assertion merely because its host is not
    registered.  A low-confidence single source is delivered as partial rather
    than silently discarded.
    """
    evidence_by_id = {item.evidence_id: item for item in evidences}
    supported = [
        item for item in claims
        if item.date_role in _DELIVERABLE_EVENT_DATE_ROLES
        and any(
            evidence_by_id[key].relevance_score >= 0.50
            and evidence_by_id[key].semantic_score >= 0.75
            and evidence_by_id[key].freshness_score > 0.0
            for key in item.evidence_ids if key in evidence_by_id
        )
    ]
    eligible = [item for item in supported if _future_claim_is_fresh(item, evidence_by_id, now)]
    if not eligible:
        # A past schedule is not a final fact by itself, but it may still
        # provide an explicit market for a separately sourced actual-release
        # statement with the *same* work and date.  Keep it strictly as a
        # corroborator; it cannot produce a result on its own.
        if not any(item.date_role == ACTUAL_RELEASE for item in supported):
            return "NO_AUTHORITATIVE_SOURCE", []

    # A single source that appears to support several final values is an
    # extraction ambiguity, not independent evidence of a factual conflict.
    values_by_evidence: dict[str, set[str]] = {}
    for claim in eligible:
        for evidence_id in claim.evidence_ids:
            values_by_evidence.setdefault(evidence_id, set()).add(claim.value)
    ambiguous_evidence_ids = {
        evidence_id for evidence_id, values in values_by_evidence.items() if len(values) > 1
    }
    eligible = [
        claim for claim in eligible
        if not any(evidence_id in ambiguous_evidence_ids for evidence_id in claim.evidence_ids)
    ]
    if not eligible:
        return "NO_AUTHORITATIVE_SOURCE", []

    # An explicit actual-release statement outranks a current schedule.  Do
    # not make an old or future schedule compete with a fact of completion.
    actual = [claim for claim in eligible if claim.date_role == ACTUAL_RELEASE]
    if actual:
        historical_schedules = [
            item for item in supported
            if item.date_role == SCHEDULED_RELEASE
            and item.value <= now.date().isoformat()
            and item.territory
        ]
        corroborated_actual = []
        for claim in actual:
            if claim.territory:
                corroborated_actual.append(claim)
                continue
            market = next((
                item for item in historical_schedules
                if item.subject == claim.subject
                and item.value == claim.value
                and not set(item.evidence_ids).intersection(claim.evidence_ids)
            ), None)
            if market:
                corroborated_actual.append(replace(
                    claim,
                    territory=market.territory,
                    territory_label=market.territory_label,
                    evidence_ids=tuple(dict.fromkeys((*claim.evidence_ids, *market.evidence_ids))),
                    evidence_type="CORROBORATED_EVENT_EVIDENCE",
                ))
            else:
                corroborated_actual.append(claim)
        eligible = corroborated_actual

    by_territory: dict[str, set[str]] = {}
    for claim in eligible:
        # An omitted region is not evidence that two dates concern the same
        # market.  Preserve these as partial facts but never fabricate a
        # conflict with an explicitly regional claim.
        if claim.territory:
            by_territory.setdefault(claim.territory, set()).add(claim.value)
    if any(len(values) > 1 for values in by_territory.values()):
        return "CONFLICTING", eligible

    target = assessment.territory
    target_claims = [claim for claim in eligible if target and claim.territory == target]
    if target_claims:
        return ("VERIFIED" if _aggregate_claim_confidence(target_claims, evidence_by_id) >= 0.94 else "PARTIALLY_VERIFIED"), target_claims

    # A real date can be shown only with its proven territory (or the explicit
    # "地区待确认" label), never silently substituted for the default region.
    return "PARTIALLY_VERIFIED", eligible


def _aggregate_claim_confidence(claims: list[ResearchClaim], evidence_by_id: dict[str, Evidence]) -> float:
    """Combine independent hosts without letting duplicate SERP cards inflate confidence."""
    by_host: dict[str, float] = {}
    for claim in claims:
        for evidence_id in claim.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if not evidence:
                continue
            host = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(evidence.canonical_url).hostname or evidence_id
            score = evidence.evidence_confidence or claim.confidence
            by_host[host] = max(by_host.get(host, 0.0), score)
    remaining = 1.0
    for score in by_host.values():
        remaining *= 1.0 - max(0.0, min(0.98, score))
    return 1.0 - remaining


def evaluate_generic_evidence(
    evidences: list[Evidence],
    *,
    fact_intent: str,
    policies: list[SourcePolicy],
    now: datetime,
) -> str:
    # Use evidence signals produced after every provider/detail read.  The
    # fallback freshness helper preserves compatibility with historic rows.
    eligible = [
        item for item in evidences
        if item.relevance_score >= 0.45
        and item.semantic_score >= 0.50
        and item.freshness_score > 0.0
        and evidence_is_fresh(item, fact_intent=fact_intent, now=now)
    ]
    if not eligible:
        return "NO_AUTHORITATIVE_SOURCE"
    if any("冲突" in item.snippet or "conflict" in item.snippet.lower() for item in eligible):
        return "CONFLICTING"
    return "VERIFIED" if _aggregate_evidence_confidence(eligible) >= 0.85 else "PARTIALLY_VERIFIED"


def _aggregate_evidence_confidence(evidences: list[Evidence]) -> float:
    by_host: dict[str, float] = {}
    for item in evidences:
        host = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(item.canonical_url).hostname or item.evidence_id
        fallback = {"OFFICIAL": 0.98, "PRIMARY": 0.94, "PUBLISHER": 0.86}.get(item.source_tier, item.source_reputation)
        by_host[host] = max(by_host.get(host, 0.0), item.evidence_confidence or fallback)
    remaining = 1.0
    for score in by_host.values():
        remaining *= 1.0 - max(0.0, min(0.98, score))
    return 1.0 - remaining


def extract_policy_appointment_claims(
    evidences: list[Evidence], *, query: str, policies: list[SourcePolicy], now: datetime,
) -> list[ResearchClaim]:
    """Extract only explicit current-office-holder statements from one field."""
    del now
    normalized_query = re.sub(r"[？?。！!]+$", "", query).strip()
    subject_match = re.search(r"([^，。；;]{1,24}(?:总统|总理|首相|部长|负责人))", normalized_query)
    subject = subject_match.group(1).strip() if subject_match else normalized_query[:80]
    output: list[ResearchClaim] = []
    seen: set[tuple[str, str]] = set()
    patterns = (
        re.compile(rf"{re.escape(subject)}(?:现任|当前)?(?:是|为)\s*([\u4e00-\u9fffA-Za-z·•・\- ]{{2,48}})"),
        re.compile(rf"现任{re.escape(subject)}(?:是|为)?\s*([\u4e00-\u9fffA-Za-z·•・\- ]{{2,48}})"),
    )
    for evidence in evidences:
        for text, locator in ((evidence.title, "TITLE"), (evidence.snippet, evidence.extraction_locator_type or "SERP_SNIPPET")):
            if not text or subject not in text:
                continue
            value = ""
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    value = match.group(1).strip(" ，,。；;")
                    break
            if not value:
                continue
            key = (evidence.evidence_id, value)
            if key in seen:
                continue
            seen.add(key)
            output.append(ResearchClaim(
                claim_id=_claim_id(evidence.evidence_id, value, None), subject=subject,
                predicate="CURRENT_OFFICE_HOLDER", value=value, territory=None, territory_label=None,
                event_state="CURRENT", evidence_ids=(evidence.evidence_id,), source_tier=evidence.source_tier,
                source_policy_id=evidence.source_policy_id,
                claim_status="CANDIDATE",
                confidence=evidence.evidence_confidence or {
                    "OFFICIAL": 0.98, "PRIMARY": 0.94, "PUBLISHER": 0.86,
                }.get(evidence.source_tier, evidence.source_reputation),
                evidence_type=evidence.evidence_type, extraction_locator_type=locator,
            ))
    return output
