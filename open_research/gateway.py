"""Tavily-only adapter used by Open Research P0."""

from __future__ import annotations

from typing import Any, Protocol

from web_search import WebSearchClient, WebSearchError


class SearchGateway(Protocol):
    def search(
        self,
        query: str,
        *,
        freshness: str,
        topic: str,
        include_domains: tuple[str, ...] = (),
    ) -> dict[str, Any]: ...


class TavilyGateway:
    """Rejects non-Tavily clients even if legacy OPEN_QA supports them."""

    def __init__(self, client: WebSearchClient):
        self.client = client

    def search(
        self,
        query: str,
        *,
        freshness: str,
        topic: str,
        include_domains: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if self.client.provider != "tavily":
            raise ResearchGatewayError("SEARCH_PROVIDER_NOT_APPROVED")
        try:
            return self.client.search(query, freshness=freshness, topic=topic, include_domains=list(include_domains))
        except WebSearchError as exc:
            mapping = {
                "WEB_SEARCH_UNAVAILABLE": "SEARCH_UNAVAILABLE",
                "WEB_SEARCH_PROVIDER_REJECTED": "SEARCH_RATE_LIMITED",
                "WEB_SEARCH_NOT_CONFIGURED": "SEARCH_UNAVAILABLE",
                "WEB_SEARCH_POLICY_BLOCKED": "RESEARCH_EGRESS_BLOCKED",
            }
            raise ResearchGatewayError(mapping.get(exc.code, "SEARCH_UNAVAILABLE")) from exc


class ResearchGatewayError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
