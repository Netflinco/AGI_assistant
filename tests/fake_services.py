"""No-network fakes used by Open Research / Office P0 regression scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class FakeTavilyGateway:
    def __init__(self, citations: list[dict[str, Any]] | None = None, error: str | None = None):
        self.calls: list[dict[str, Any]] = []
        self.citations = citations or []
        self.error = error

    def search(self, query: str, *, freshness: str, topic: str, include_domains: tuple[str, ...] = ()) -> dict[str, Any]:
        self.calls.append({"query": query, "freshness": freshness, "topic": topic, "include_domains": list(include_domains)})
        if self.error:
            from open_research.gateway import ResearchGatewayError
            raise ResearchGatewayError(self.error)
        return {"provider": "tavily", "request_id": "fake_req", "fetched_at": "2026-08-18T12:00:00+00:00", "citations": self.citations}


class FakeDetailFetcher:
    """In-memory detail reader for G2R tests; it never performs a request."""

    def __init__(self, by_url: dict[str, str] | None = None, *, status: str = "DETAIL_FETCHED"):
        self.by_url = by_url or {}
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def fetch(self, url: str, *, entity: str, predicates: tuple[str, ...]):
        from open_research.detail_fetch import DetailResult
        self.calls.append({"url": url, "entity": entity, "predicates": list(predicates)})
        fragment = self.by_url.get(url, "")
        return DetailResult(self.status if fragment else "DETAIL_NO_FACT", fragment=fragment, locator_type="BODY_PARAGRAPH" if fragment else None)


class FakeModelGateway:
    def __init__(self, spec: dict | None = None):
        self.calls: list[dict[str, Any]] = []
        self.spec = spec

    def create_spec(self, *, title: str, fragments: dict, brief: dict | None) -> dict:
        self.calls.append({"title": title, "fragments": fragments, "brief": brief})
        if self.spec is not None:
            return self.spec
        from office_agent.specs import deterministic_spec
        return deterministic_spec(title, fragments, brief)


class FakeScanner:
    def __init__(self, result: str = "CLEAN"):
        self.result = result
        self.calls: list[str] = []

    def scan(self, filename: str, content: bytes) -> str:
        self.calls.append(filename)
        return self.result


def fake_renderer(pptx_path: str | Path, output_dir: str | Path) -> Path:
    pdf = Path(output_dir) / (Path(pptx_path).stem + ".pdf")
    from pypdf import PdfWriter
    from pptx import Presentation
    writer = PdfWriter()
    for _slide in Presentation(str(pptx_path)).slides:
        writer.add_blank_page(width=960, height=540)
    with pdf.open("wb") as handle:
        writer.write(handle)
    return pdf


def fake_preview(pdf_path: str | Path) -> Path:
    """A valid, non-blank PNG used by quality-gate unit tests."""
    from PIL import Image, ImageDraw
    png = Path(pdf_path).with_suffix(".png")
    image = Image.new("RGB", (320, 180), (248, 250, 252))
    ImageDraw.Draw(image).rectangle((20, 20, 300, 150), fill=(22, 78, 99))
    image.save(png, format="PNG")
    return png
