"""High-confidence domain routing and conservative entity/query rewriting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


_OFFICE_TERMS = ("ppt", "powerpoint", "演示文稿", "幻灯片", "excel", "word", "文档转换", "做成报告", "图表")
_INSPECTION_TERMS = ("巡检", "门店", "店铺", "告警", "摄像头", "监控", "录像", "快照", "离岗", "抽烟", "消防", "deepvision")
_DYNAMIC_TERMS = (
    "最新", "当前", "现在", "今日", "今天", "实时", "刚刚", "上映", "发布", "版本", "价格", "股价", "汇率",
    "政策", "法规", "任命", "是谁", "活动", "比赛", "航班", "天气", "路况", "通报", "何时", "什么时候",
    "是否", "有没有", "情况", "进展", "推荐", "比较", "对比", "联网", "核验",
)
_RESEARCH_EVENT_TERMS = ("上映", "影视", "发布", "政策", "法规", "任命", "通报", "活动", "比赛", "版本", "进展")
_EXPLICIT_RESEARCH_TERMS = ("联网", "核验", "公开信息", "公开检索")
_COLLABORATION_TERMS = ("并做", "做成", "生成", "整理成", "输出")


@dataclass(frozen=True)
class EntityRewrite:
    original_query: str
    rewritten_query: str
    confidence: float
    reason: str
    candidates: tuple[str, ...] = ()

    @property
    def applied(self) -> bool:
        return self.rewritten_query != self.original_query and self.confidence >= 0.92 and len(self.candidates) <= 1

    def public_dict(self) -> dict[str, Any]:
        return {**asdict(self), "candidates": list(self.candidates), "applied": self.applied}


class EntityResolver:
    """Conservative correction layer before query planning.

    It accepts a controlled alias catalogue so a deployment can upgrade it to a
    dictionary/entity service later.  The built-in record preserves the
    screenshot bad-case and demonstrates the `HOMOPHONIC_TYPO` contract.  A
    candidate is never silently chosen when confidence is low or tied.
    """

    AUTO_REWRITE_THRESHOLD = 0.92
    # Bootstrap records keep the screenshot bad-case reproducible in a fresh
    # local installation.  Production aliases are loaded from the tenant
    # controlled registry by ``server.open_research_service_for_request``;
    # this resolver deliberately has no network or model dependency.
    DEFAULT_ALIASES = {
        "长安的离职": ("长安的荔枝", 0.98, "HOMOPHONIC_TYPO"),
    }

    def __init__(self, aliases: dict[str, tuple[str, float, str]] | None = None):
        self.aliases = {**self.DEFAULT_ALIASES, **(aliases or {})}

    def resolve(self, query: str, candidates: list[tuple[str, float]] | None = None) -> EntityRewrite:
        normalized = re.sub(r"\s+", " ", str(query or "")).strip()
        if candidates:
            ordered = sorted(candidates, key=lambda item: item[1], reverse=True)
            best, score = ordered[0]
            tied = len(ordered) > 1 and abs(ordered[0][1] - ordered[1][1]) < 0.03
            if score >= self.AUTO_REWRITE_THRESHOLD and not tied:
                return EntityRewrite(normalized, normalized.replace(self._entity_fragment(normalized), best), score, "ENTITY_RESOLUTION", (best,))
            return EntityRewrite(normalized, normalized, score, "ENTITY_AMBIGUOUS" if tied else "ENTITY_LOW_CONFIDENCE", tuple(item[0] for item in ordered[:3]))
        matches = [
            (source, target, float(confidence), reason)
            for source, (target, confidence, reason) in self.aliases.items()
            if source and source in normalized and source != target
        ]
        if matches:
            # A registry can contain an overlapping typo and alias.  Never
            # let insertion order decide a rewritten public query: an equal
            # confidence, different-target match must ask the user instead.
            matches.sort(key=lambda item: (item[2], len(item[0])), reverse=True)
            source, target, confidence, reason = matches[0]
            alternatives = list(dict.fromkeys(item[1] for item in matches if item[1] != target))
            tied = bool(alternatives) and abs(confidence - next(item[2] for item in matches if item[1] != target)) < 0.03
            if tied:
                return EntityRewrite(normalized, normalized, confidence, "ENTITY_AMBIGUOUS", tuple([target, *alternatives][:3]))
            if confidence < self.AUTO_REWRITE_THRESHOLD:
                return EntityRewrite(normalized, normalized, confidence, "ENTITY_LOW_CONFIDENCE", (target,))
            return EntityRewrite(normalized, normalized.replace(source, target), confidence, reason, (target,))
        return EntityRewrite(normalized, normalized, 1.0, "NO_REWRITE", ())

    @staticmethod
    def _entity_fragment(query: str) -> str:
        title = re.search(r"《([^》]+)》", query)
        return title.group(1) if title else query


class DomainRouter:
    """Candidate-only router.  It performs no IO and never calls a provider."""

    @staticmethod
    def _result(domain: str, confidence: float, reason: str, workflow: bool = False,
                *, task_type: str | None = None, evidence_required: bool = False,
                time_requirement: str = "NONE") -> dict[str, Any]:
        """Return an explicit routing contract instead of a bare domain label.

        The router is deliberately not an LLM decision point.  Downstream code
        can therefore tell the difference between a dynamic ``EVENT_STATUS``
        task, a general fallback answer, and a document-generation task before
        it creates any provider, parser, or worker side effect.
        """
        return {
            "domain": domain,
            "confidence": confidence,
            "reason": reason,
            "workflow": workflow,
            "task_type": task_type or domain,
            "evidence_required": evidence_required,
            "time_requirement": time_requirement,
        }

    def classify(self, text: str, *, mode_override: str = "AUTO", attachment_ids: list[str] | None = None) -> dict[str, Any]:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        lower = normalized.lower()
        mode = str(mode_override or "AUTO").upper()
        attachment_ids = attachment_ids or []
        if mode == "INSPECTION":
            return self._result("INSPECTION", 1.0, "MODE_LOCK", task_type="INSPECTION")
        if any(term in lower for term in _INSPECTION_TERMS):
            return self._result("INSPECTION", 0.98, "INSPECTION_SEMANTICS", task_type="INSPECTION")
        office = bool(attachment_ids) or any(term in lower for term in _OFFICE_TERMS)
        # Do not seize legacy OPEN_QA's existing weather/traffic/finance
        # experience merely because it contains a time word.  A new-domain
        # route needs a public-event predicate, an explicit request to verify
        # online, or the quoted-work event pattern from the screenshot.  This
        # keeps the routing point narrow and preserves the prior product
        # contract while still covering generic public event/status queries.
        research = (
            any(term in normalized for term in _RESEARCH_EVENT_TERMS)
            or any(term in normalized for term in _EXPLICIT_RESEARCH_TERMS)
            or bool(re.search(r"《[^》]{1,50}》.*(?:何时|什么时候|上映|发布)", normalized))
        )
        office_egress = bool(attachment_ids) and any(term in normalized for term in ("竞品", "搜索", "检索", "联网", "外网"))
        if office_egress:
            return self._result("OFFICE_EGRESS", 0.98, "OFFICE_TO_RESEARCH_REQUEST", task_type="OFFICE_TO_RESEARCH")
        if office and research and any(term in normalized for term in _COLLABORATION_TERMS):
            return self._result("HYBRID", 0.97, "EXPLICIT_RESEARCH_TO_OFFICE", True,
                                task_type="RESEARCH_TO_OFFICE", evidence_required=True, time_requirement="DYNAMIC")
        if office and research:
            return self._result("CLARIFY", 0.65, "AMBIGUOUS_CROSS_DOMAIN", task_type="CROSS_DOMAIN_CLARIFICATION")
        if office:
            return self._result("OFFICE", 0.96, "OFFICE_TARGET_OR_ATTACHMENT", task_type="OFFICE_TO_PPT")
        if research:
            event_status = bool(re.search(r"《[^》]{1,50}》.*(?:何时|什么时候|上映|发布)", normalized))
            return self._result("OPEN_RESEARCH", 0.95, "DYNAMIC_PUBLIC_FACT", task_type="EVENT_STATUS" if event_status else "PUBLIC_DYNAMIC_FACT",
                                evidence_required=True, time_requirement="EVENT" if event_status else "DYNAMIC")
        return self._result("FALLBACK", 0.0, "NOT_A_NEW_DOMAIN", task_type="OPEN_QA")
