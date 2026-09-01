"""Strict SlideSpec v1 schema and deterministic fallback planner."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .policy import OfficePolicyError


ALLOWED_LAYOUTS = {"cover", "agenda", "kpi", "summary", "sources"}
ALLOWED_CHARTS = {"bar", "line", "none"}
_MAX_BODY_CHARS = {"cover": 240, "agenda": 700, "kpi": 700, "summary": 900, "sources": 1_200}


def deterministic_spec(title: str, fragments: dict[str, Any], brief: dict | None = None) -> dict[str, Any]:
    slides = [{"layout": "cover", "title": title[:80], "body": "管理层汇报", "sources": []}]
    metrics = [
        {**item, "label": str(item.get("label") or "")[:32], "value": str(item.get("value") or "")[:48]}
        for item in (fragments.get("metrics") or [])
        if isinstance(item, dict)
    ]
    if metrics:
        slides.append({"layout": "kpi", "title": "关键指标", "body": "数据来自已授权文档片段", "metrics": metrics[:8], "chart": "bar", "sources": [],
                       "asset_sources": [str(item.get("source") or "") for item in metrics[:8] if item.get("source")]})
    else:
        body = "\n".join((fragments.get("headings") or fragments.get("paragraphs") or ["未提取到可展示摘要"])[:5])
        slides.append({"layout": "summary", "title": "核心摘要", "body": body[:1000], "sources": []})
    if brief:
        claims = brief.get("claims") or []
        citations = brief.get("citations") or []
        partial = brief.get("answer_status") == "PARTIALLY_VERIFIED"
        body = "\n".join(
            ("【待核验】" if partial and claim.get("claim_status") != "VERIFIED" else "") + str(claim.get("text", ""))
            for claim in claims[:4]
        )
        slides.append({"layout": "summary", "title": "公开信息核验", "body": body[:1000], "sources": [item.get("evidence_id") for item in citations]})
        prefix = "待核验 · " if partial else ""
        slides.append({"layout": "sources", "title": f"{prefix}来源与截至时间：{brief.get('as_of')}", "body": "", "sources": [item.get("evidence_id") for item in citations]})
    return {"schema_version": "SlideSpec.v1", "title": title[:80], "template_id": "template_default", "slides": slides}


def validate_spec(spec: dict[str, Any], *, brief: dict | None = None) -> dict[str, Any]:
    if not isinstance(spec, dict) or spec.get("schema_version") != "SlideSpec.v1":
        raise OfficePolicyError("OFFICE_SPEC_INVALID")
    slides = spec.get("slides")
    if not isinstance(slides, list) or not 1 <= len(slides) <= 30:
        raise OfficePolicyError("OFFICE_SPEC_INVALID")
    evidence_ids = {item.get("evidence_id") for item in (brief or {}).get("citations", [])}
    used_evidence_ids: set[str] = set()
    for slide in slides:
        if not isinstance(slide, dict) or slide.get("layout") not in ALLOWED_LAYOUTS:
            raise OfficePolicyError("OFFICE_SPEC_INVALID")
        layout = str(slide.get("layout") or "")
        if len(str(slide.get("title") or "")) > 160 or len(str(slide.get("body") or "")) > _MAX_BODY_CHARS.get(layout, 0):
            raise OfficePolicyError("OFFICE_SPEC_INVALID")
        if slide.get("chart", "none") not in ALLOWED_CHARTS:
            raise OfficePolicyError("OFFICE_SPEC_INVALID")
        if any(key in slide for key in ("path", "shell", "sql", "credential", "asset_url")):
            raise OfficePolicyError("OFFICE_SPEC_INVALID")
        sources = slide.get("sources") or []
        if not isinstance(sources, list) or not set(sources).issubset(evidence_ids):
            raise OfficePolicyError("OFFICE_SPEC_INVALID")
        used_evidence_ids.update(sources)
        asset_sources = slide.get("asset_sources") or []
        if not isinstance(asset_sources, list) or len(asset_sources) > 24 or any(not isinstance(item, str) or len(item) > 180 for item in asset_sources):
            raise OfficePolicyError("OFFICE_SPEC_INVALID")
        metrics = slide.get("metrics") or []
        if not isinstance(metrics, list) or len(metrics) > 8:
            raise OfficePolicyError("OFFICE_SPEC_INVALID")
        for item in metrics:
            if not isinstance(item, dict) or len(str(item.get("label") or "")) > 32 or len(str(item.get("value") or "")) > 48:
                raise OfficePolicyError("OFFICE_SPEC_INVALID")
    if brief:
        if not any(slide.get("layout") == "sources" for slide in slides):
            raise OfficePolicyError("OFFICE_SPEC_UNCITED_RESEARCH")
        for claim in brief.get("claims") or []:
            claim_evidence = set(claim.get("evidence_ids") or [])
            if not claim_evidence or not claim_evidence.issubset(used_evidence_ids):
                raise OfficePolicyError("OFFICE_SPEC_UNCITED_RESEARCH")
    return spec
