"""Bounded, SSRF-safe detail evidence retrieval for Open Research.

The search provider is allowed to discover a public URL; this module may read
that *already returned* URL only.  It never follows a user supplied URL, never
uses cookies or JavaScript, and keeps page bytes in memory for the duration of
one request.  Callers receive at most one cleaned fact window, not HTML.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import ipaddress
import re
import socket
from typing import Protocol
from urllib import error, request
from urllib.parse import urljoin, urlparse

from .evidence import safe_public_url


MAX_REDIRECTS = 3
MAX_BYTES = 1_000_000
MAX_FRAGMENT_CHARS = 300
TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class DetailResult:
    status: str
    fragment: str = ""
    locator_type: str | None = None
    rejection_reason: str | None = None


class DetailFetcher(Protocol):
    def fetch(self, url: str, *, entity: str, predicates: tuple[str, ...]) -> DetailResult: ...


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class SafeDetailFetcher:
    """Read a small text/html response with redirect and DNS re-validation."""

    def __init__(self, *, urlopen=None):
        self._opener = request.build_opener(_NoRedirect())
        self._urlopen = urlopen

    @staticmethod
    def _public_dns(hostname: str) -> bool:
        """Resolve before every request; any non-public answer blocks it."""
        try:
            addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return False
        if not addresses:
            return False
        for item in addresses:
            try:
                address = ipaddress.ip_address(item[4][0])
            except ValueError:
                return False
            if (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_reserved or address.is_multicast or address.is_unspecified):
                return False
        return True

    def _open(self, req: request.Request):
        if self._urlopen:
            return self._urlopen(req, TIMEOUT_SECONDS)
        return self._opener.open(req, timeout=TIMEOUT_SECONDS)

    @staticmethod
    def _clean_html(raw: str) -> str:
        raw = re.sub(r"(?is)<(script|style|noscript|svg|iframe|form|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?is)<br\s*/?>|</(?:p|div|li|tr|h[1-6])>", "\n", raw)
        return re.sub(r"\s+", " ", unescape(re.sub(r"(?is)<[^>]+>", " ", raw))).strip()

    @staticmethod
    def _fragment(text: str, *, entity: str, predicates: tuple[str, ...]) -> str:
        if not text or not entity:
            return ""
        # A fact window must contain both the requested entity and the asked
        # relation.  This deterministic first pass also ensures a page cannot
        # lend another work's date to the target work.
        for match in re.finditer(re.escape(entity), text, flags=re.I):
            left, right = max(0, match.start() - 150), min(len(text), match.end() + 150)
            window = text[left:right]
            if any(predicate in window for predicate in predicates):
                return window[:MAX_FRAGMENT_CHARS].strip()
        return ""

    def fetch(self, url: str, *, entity: str, predicates: tuple[str, ...]) -> DetailResult:
        current = safe_public_url(url)
        if not current:
            return DetailResult("REJECTED", rejection_reason="UNSAFE_URL")
        for _attempt in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if not parsed.hostname or not self._public_dns(parsed.hostname):
                return DetailResult("REJECTED", rejection_reason="DNS_NOT_PUBLIC")
            req = request.Request(current, method="GET", headers={
                "Accept": "text/html, text/plain;q=0.9",
                "User-Agent": "DeepVision-OpenResearch/1.0",
            })
            try:
                response = self._open(req)
                # Test fetchers may return a bytes object; production returns
                # an HTTPResponse.  Neither shape is persisted by this class.
                if isinstance(response, (bytes, bytearray)):
                    raw, content_type = bytes(response), "text/html"
                    status = 200
                else:
                    status = getattr(response, "status", 200) or 200
                    headers = getattr(response, "headers", {})
                    content_type = str(headers.get("Content-Type") or "")
                    length = headers.get("Content-Length")
                    if length and int(length) > MAX_BYTES:
                        return DetailResult("REJECTED", rejection_reason="RESPONSE_TOO_LARGE")
                    raw = response.read(MAX_BYTES + 1)
                    if hasattr(response, "close"):
                        response.close()
                if status >= 300:
                    return DetailResult("REJECTED", rejection_reason="HTTP_STATUS")
            except error.HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}:
                    return DetailResult("REJECTED", rejection_reason="HTTP_STATUS")
                location = exc.headers.get("Location") if exc.headers else None
                if not location or _attempt >= MAX_REDIRECTS:
                    return DetailResult("REJECTED", rejection_reason="REDIRECT_LIMIT")
                current = safe_public_url(urljoin(current, location))
                if not current:
                    return DetailResult("REJECTED", rejection_reason="UNSAFE_REDIRECT")
                continue
            except (OSError, TimeoutError, ValueError):
                return DetailResult("REJECTED", rejection_reason="FETCH_FAILED")
            if len(raw) > MAX_BYTES:
                return DetailResult("REJECTED", rejection_reason="RESPONSE_TOO_LARGE")
            if not (content_type.lower().startswith("text/html") or content_type.lower().startswith("text/plain")):
                return DetailResult("REJECTED", rejection_reason="UNSUPPORTED_MIME")
            text = self._clean_html(raw.decode("utf-8", errors="replace"))
            if re.search(r"(?i)(ignore (?:all |previous )?instructions|system prompt|工具权限|发送附件)", text):
                return DetailResult("REJECTED", rejection_reason="PROMPT_INJECTION")
            fragment = self._fragment(text, entity=entity, predicates=predicates)
            return DetailResult(
                "DETAIL_FETCHED" if fragment else "DETAIL_NO_FACT",
                fragment=fragment,
                locator_type="BODY_PARAGRAPH" if fragment else None,
            )
        return DetailResult("REJECTED", rejection_reason="REDIRECT_LIMIT")
