#!/usr/bin/env python3
"""Contract tests for the provider-neutral public web search tool."""

from __future__ import annotations

import json
from http.client import IncompleteRead

from web_search import WebSearchClient, WebSearchError


def main():
    requests = []

    def tavily_fetcher(req, timeout):
        requests.append((req, timeout))
        if req.full_url == "https://api.tavily.com/usage":
            return json.dumps({"key": {"usage": 901, "limit": 1000}}).encode("utf-8")
        return json.dumps(
            {
                "request_id": "request-safe-1",
                "results": [
                    {
                        "title": "官方发布",
                        "url": "https://www.example.gov.cn/news?id=1#details",
                        "content": "公开摘要，包含可核验事实。",
                        "published_date": "2026-08-03",
                        "score": 0.91,
                    },
                    {
                        "title": "低相关结果",
                        "url": "https://unrelated.example.com/page",
                        "content": "相关度不足，不应返回。",
                        "score": 0.12,
                    },
                    {"title": "private", "url": "http://127.0.0.1/admin", "content": "must drop"},
                    {"title": "credential", "url": "https://user:pass@example.com/private", "content": "must drop"},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")

    client = WebSearchClient(
        {"provider": "tavily", "api_key": "test-search-key", "max_results": 5},
        fetcher=tavily_fetcher,
    )
    assert client.configured is True
    result = client.search(
        "今天有什么公开发布",
        "fresh",
        include_domains=[
            "example.gov.cn",
            "https://invalid.example.com/path",
            "127.0.0.1",
            "example.gov.cn",
        ],
    )
    assert result["provider"] == "tavily"
    assert result["request_id"] == "request-safe-1"
    assert result["citations"] == [
        {
            "title": "官方发布",
            "url": "https://www.example.gov.cn/news",
            "snippet": "公开摘要，包含可核验事实。",
            "published_at": "2026-08-03",
            "domain": "www.example.gov.cn",
        }
    ]
    request_payload = json.loads(requests[0][0].data.decode("utf-8"))
    assert request_payload["topic"] == "news"
    assert request_payload["query"] == "今天有什么公开发布"
    assert request_payload["include_usage"] is True
    assert request_payload["include_domains"] == ["example.gov.cn"]
    assert "api_key" not in request_payload
    assert requests[0][0].get_header("Authorization") == "Bearer test-search-key"
    assert requests[0][1] <= 10
    weather_result = client.search("2026年8月4日杭州天气", "day", topic="general")
    weather_payload = json.loads(requests[1][0].data.decode("utf-8"))
    assert weather_result["topic"] == "general"
    assert weather_result["freshness"] == "day"
    assert weather_payload["topic"] == "general"
    assert weather_payload["time_range"] == "day"
    assert weather_payload["search_depth"] == "basic"
    usage = client.usage()
    assert usage == {
        "used_credits": 901,
        "credit_limit": 1000,
        "remaining_credits": 99,
        "usage_scope": "key",
        "reported_at": usage["reported_at"],
    }

    try:
        client.search("api_key=sk-12345678901234567890")
        raise AssertionError("secret-like query must not leave the process")
    except WebSearchError as exc:
        assert exc.code == "WEB_SEARCH_POLICY_BLOCKED"
    assert len(requests) == 3, "sensitive input must not issue another provider request"

    # A provider can terminate a chunked response after sending headers.  This
    # must degrade to a stable availability error rather than escape as a 500.
    incomplete = WebSearchClient(
        {"provider": "tavily", "api_key": "test-search-key"},
        fetcher=lambda _req, _timeout: (_ for _ in ()).throw(IncompleteRead(b"", 128)),
    )
    try:
        incomplete.search("公开事实核验")
        raise AssertionError("incomplete provider response must be normalized")
    except WebSearchError as exc:
        assert exc.code == "WEB_SEARCH_UNAVAILABLE"

    brave_requests = []

    def brave_fetcher(req, timeout):
        brave_requests.append((req, timeout))
        return json.dumps(
            {
                "query": {"original": "美国总统是谁"},
                "web": {
                    "results": [
                        {
                            "title": "Public profile",
                            "url": "https://www.whitehouse.gov/administration/",
                            "description": "Official public profile.",
                            "page_age": "2026-08-01",
                        }
                    ]
                },
            }
        ).encode("utf-8")

    brave = WebSearchClient(
        {"provider": "brave", "api_key": "brave-test-key", "max_results": 2, "country": "US", "search_lang": "zh-hans"},
        fetcher=brave_fetcher,
    )
    brave_result = brave.search("美国总统是谁", "fresh")
    assert brave_result["citations"][0]["domain"] == "www.whitehouse.gov"
    assert brave_requests[0][0].get_header("X-subscription-token") == "brave-test-key"
    assert "country=US" in brave_requests[0][0].full_url
    assert "search_lang=zh-hans" in brave_requests[0][0].full_url

    assert WebSearchClient({"provider": "tavily"}).configured is False
    print("PASS web search tests: provider contracts, secret boundary, safe citations")


if __name__ == "__main__":
    main()
