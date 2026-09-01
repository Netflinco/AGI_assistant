#!/usr/bin/env python3
"""Read-only public web search adapter for the isolated open-QA path.

The adapter intentionally accepts only provider-owned HTTPS endpoints. It never
receives tenant context, conversation history, credentials other than its own
provider key, or arbitrary caller-supplied URLs. Returned results are reduced
to a small citation contract before they can reach the model or the UI.
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timezone
from http.client import IncompleteRead
from typing import Callable
from urllib import error, request
from urllib.parse import urlencode, urlparse, urlunparse


MAX_QUERY_CHARS = 320
MAX_RESULTS = 8
MAX_TITLE_CHARS = 180
MAX_SNIPPET_CHARS = 600
MAX_INCLUDE_DOMAINS = 8
MIN_TAVILY_RELEVANCE_SCORE = 0.5
_ALLOWED_PROVIDERS = {"tavily", "brave"}
_ALLOWED_TOPICS = {"general", "news", "finance"}
_ALLOWED_TIME_RANGES = {"day", "week", "month", "year", "d", "w", "m", "y"}
_PROVIDER_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
}
_TAVILY_USAGE_ENDPOINT = "https://api.tavily.com/usage"
_SENSITIVE_QUERY_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk|pk)-[a-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|app[_-]?secret|password|authorization)\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._-]{12,}"),
    re.compile(r"(?<!\d)1\d{10}(?!\d)"),
)
_DOMAIN_PATTERN = re.compile(
    r"(?i)^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class WebSearchError(Exception):
    """A safe, provider-neutral web-search failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _safe_public_url(value) -> str:
    """Remove tracking data and reject local/private or credentialed URLs."""
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        return ""
    if host.endswith(".local") or host.endswith(".internal"):
        return ""
    clean = parsed._replace(params="", query="", fragment="")
    return urlunparse(clean)


class WebSearchClient:
    """Minimal provider adapter with a stable citation-only output contract."""

    def __init__(self, config: dict | None = None, fetcher: Callable | None = None):
        config = config or {}
        self.provider = str(config.get("provider") or "").strip().lower()
        self.api_key = str(config.get("api_key") or "").strip()
        self.max_results = _bounded_int(config.get("max_results") or 5, 5, 1, MAX_RESULTS)
        self.country = _text(config.get("country"), 8).upper()
        self.search_lang = _text(config.get("search_lang"), 16).lower()
        self.timeout_seconds = _bounded_int(config.get("timeout_seconds") or 8, 8, 1, 10)
        self._fetcher = fetcher or self._urlopen

    @property
    def configured(self) -> bool:
        return self.provider in _ALLOWED_PROVIDERS and bool(self.api_key)

    @property
    def public_config(self) -> dict:
        return {
            "configured": self.configured,
            "provider": self.provider if self.provider in _ALLOWED_PROVIDERS else None,
            "max_results": self.max_results,
            "country": self.country,
            "search_lang": self.search_lang,
            "timeout_seconds": self.timeout_seconds,
        }

    @staticmethod
    def _urlopen(req: request.Request, timeout: int) -> bytes:
        with request.urlopen(req, timeout=timeout) as response:
            return response.read()

    def search(
        self,
        query: str,
        freshness: str | None = None,
        topic: str | None = None,
        include_domains: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        cleaned_query = _text(query, MAX_QUERY_CHARS)
        if not cleaned_query:
            raise WebSearchError("WEB_SEARCH_INVALID_QUERY", "检索问题不能为空")
        if any(pattern.search(cleaned_query) for pattern in _SENSITIVE_QUERY_PATTERNS):
            raise WebSearchError("WEB_SEARCH_POLICY_BLOCKED", "问题中可能包含敏感信息，未发送至公共搜索服务")
        if not self.configured:
            raise WebSearchError("WEB_SEARCH_NOT_CONFIGURED", "公共搜索服务尚未配置")
        try:
            result = self._request_provider(
                cleaned_query,
                freshness,
                topic,
                self._safe_domains(include_domains),
            )
        except error.HTTPError as exc:
            raise WebSearchError("WEB_SEARCH_PROVIDER_REJECTED", "公共搜索服务拒绝了本次请求") from exc
        except (error.URLError, TimeoutError, OSError, IncompleteRead) as exc:
            raise WebSearchError("WEB_SEARCH_UNAVAILABLE", "公共搜索服务暂时不可用") from exc
        except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            raise WebSearchError("WEB_SEARCH_INVALID_RESPONSE", "公共搜索服务返回异常") from exc
        return result

    def usage(self) -> dict | None:
        """Return the Tavily key balance without exposing the provider credential."""
        if self.provider != "tavily":
            return None
        if not self.configured:
            raise WebSearchError("WEB_SEARCH_NOT_CONFIGURED", "公共搜索服务尚未配置")
        req = request.Request(
            _TAVILY_USAGE_ENDPOINT,
            method="GET",
            headers={"Accept": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            raw = self._fetcher(req, self.timeout_seconds)
            payload = json.loads(self._bytes(raw).decode("utf-8"))
            key = payload.get("key") if isinstance(payload.get("key"), dict) else {}
            account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
            used = self._usage_int(key.get("usage"))
            limit = self._usage_int(key.get("limit"))
            scope = "key"
            # Development keys can inherit the account-level monthly pool and
            # return key.limit=null. In that case the account plan is authoritative.
            if limit is None:
                used = self._usage_int(account.get("plan_usage"))
                limit = self._usage_int(account.get("plan_limit"))
                scope = "account"
            if used is None or limit is None or limit <= 0:
                raise ValueError("invalid usage payload")
        except error.HTTPError as exc:
            raise WebSearchError("WEB_SEARCH_PROVIDER_REJECTED", "公共搜索服务拒绝了用量查询") from exc
        except (error.URLError, TimeoutError, OSError, IncompleteRead) as exc:
            raise WebSearchError("WEB_SEARCH_UNAVAILABLE", "公共搜索服务暂时不可用") from exc
        except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            raise WebSearchError("WEB_SEARCH_INVALID_RESPONSE", "公共搜索服务返回异常") from exc
        return {
            "used_credits": used,
            "credit_limit": limit,
            "remaining_credits": max(0, limit - used),
            "usage_scope": scope,
            "reported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _request_provider(
        self,
        query: str,
        freshness: str | None,
        topic: str | None,
        include_domains: list[str],
    ) -> dict:
        normalized_topic = _text(topic, 16).lower()
        selected_topic = normalized_topic if normalized_topic in _ALLOWED_TOPICS else None
        if self.provider == "tavily":
            if selected_topic is None:
                selected_topic = "news" if freshness == "fresh" else "general"
            payload = {
                "query": query,
                "search_depth": "basic",
                "topic": selected_topic,
                "max_results": self.max_results,
                "include_answer": False,
                "include_raw_content": False,
                "include_usage": True,
            }
            if freshness in _ALLOWED_TIME_RANGES:
                payload["time_range"] = freshness
            if include_domains:
                payload["include_domains"] = include_domains
            req = request.Request(
                _PROVIDER_ENDPOINTS["tavily"],
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            )
            raw = self._fetcher(req, self.timeout_seconds)
            parsed = json.loads(self._bytes(raw).decode("utf-8"))
            source_rows = parsed.get("results") if isinstance(parsed.get("results"), list) else []
            relevant_rows = [
                item
                for item in source_rows
                if isinstance(item, dict) and self._relevant_tavily_result(item)
            ]
            citations = [
                self._citation(item, title_key="title", url_key="url", snippet_key="content", date_key="published_date")
                for item in relevant_rows
            ]
            request_id = _text(parsed.get("request_id") or parsed.get("requestId"), 128)
            usage = self._usage_int((parsed.get("usage") or {}).get("credits")) if isinstance(parsed.get("usage"), dict) else None
        else:
            params = {"q": query, "count": str(self.max_results)}
            if self.country:
                params["country"] = self.country
            if self.search_lang:
                params["search_lang"] = self.search_lang
            req = request.Request(
                f"{_PROVIDER_ENDPOINTS['brave']}?{urlencode(params)}",
                method="GET",
                headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
            )
            raw = self._fetcher(req, self.timeout_seconds)
            parsed = json.loads(self._bytes(raw).decode("utf-8"))
            web = parsed.get("web") if isinstance(parsed.get("web"), dict) else {}
            source_rows = web.get("results") if isinstance(web.get("results"), list) else []
            citations = [
                self._citation(item, title_key="title", url_key="url", snippet_key="description", date_key="page_age")
                for item in source_rows
                if isinstance(item, dict)
            ]
            request_id = _text(parsed.get("query") or parsed.get("request_id"), 128)
            usage = None
        safe_citations = [item for item in citations if item][: self.max_results]
        result = {
            "query": query,
            "provider": self.provider,
            "topic": selected_topic,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request_id": request_id or None,
            "freshness": freshness or "general",
            "citations": safe_citations,
        }
        if usage is not None:
            result["usage"] = {"credits": usage}
        return result

    @staticmethod
    def _safe_domains(values: list[str] | tuple[str, ...] | None) -> list[str]:
        """Accept hostnames only; schemes, paths, IPs and local hosts are rejected."""
        if not isinstance(values, (list, tuple)):
            return []
        safe = []
        for value in values:
            domain = str(value or "").strip().lower().rstrip(".")
            if not domain or not _DOMAIN_PATTERN.fullmatch(domain):
                continue
            try:
                address = ipaddress.ip_address(domain)
            except ValueError:
                address = None
            if address is not None or domain.endswith((".local", ".internal", ".localhost")):
                continue
            if domain not in safe:
                safe.append(domain)
            if len(safe) >= MAX_INCLUDE_DOMAINS:
                break
        return safe

    @staticmethod
    def _relevant_tavily_result(item: dict) -> bool:
        """Tavily omits score in some responses; only reject an explicit low score."""
        score = item.get("score")
        if score is None:
            return True
        try:
            return float(score) >= MIN_TAVILY_RELEVANCE_SCORE
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _bytes(raw) -> bytes:
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, bytearray):
            return bytes(raw)
        raise ValueError("provider response must be bytes")

    @staticmethod
    def _usage_int(value) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @staticmethod
    def _citation(item: dict, *, title_key: str, url_key: str, snippet_key: str, date_key: str) -> dict | None:
        url = _safe_public_url(item.get(url_key))
        if not url:
            return None
        title = _text(item.get(title_key), MAX_TITLE_CHARS) or urlparse(url).hostname or "网页来源"
        snippet = _text(item.get(snippet_key), MAX_SNIPPET_CHARS)
        published_at = _text(item.get(date_key), 48) or None
        return {
            "title": title,
            "url": url,
            "snippet": snippet,
            "published_at": published_at,
            "domain": urlparse(url).hostname or "",
        }
