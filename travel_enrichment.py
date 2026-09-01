#!/usr/bin/env python3
"""Generalized, read-only travel enrichment for open-QA documents.

Travel recommendations remain source candidates rather than opaque model facts.
Destination images are restricted to Wikimedia Commons so the PDF can retain
source and license attribution alongside each reused asset.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Callable
from urllib import error, request
from urllib.parse import urlencode, urlparse, quote_plus


WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
MAX_TRAVEL_RECOMMENDATIONS = 4
MAX_TRAVEL_IMAGES = 3
MAX_IMAGE_BYTES = 2 * 1024 * 1024
_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_GENERIC_VENUE_MARKERS = (
    "accommodation", "accommodations", "travel guide", "survival guide", "visitor guide",
    "first time visitor", "traveling to", "travelling to",
    "visit ", "vacation", "vacations", "things to do", "best areas", "neighborhood guide",
    "michelin guide", "full list", "best hotels", "top hotels", "hotels in", "where to stay",
    "hotel guide", "hotel reservations", "book hotel", "best restaurants", "top restaurants",
    "restaurants in", "restaurant guide", "where to eat", "what to eat", "food guide",
    "eating like a local", "ultimate local", "recommended by", "酒店推荐", "酒店排名",
    "book now", "check rates", "room type", "how to get there", "minute walk", "minutes walk",
    "walking distance", "official website", "online booking", "select room", "立即预订", "查看房价",
    "住宿攻略", "餐厅推荐", "美食攻略", "旅游攻略", "完整名单", "榜单",
)
_ADDRESS_STOP_PATTERN = re.compile(
    r"\s+(?:walking\s+distance|walk(?:ing)?\s+time|minutes?\s+(?:on\s+foot|walk)|"
    r"mrt|metro|subway|nearest\s+station|official\s+website|website|promo(?:tion)?\s+code|"
    r"book\s+now|booking\s+guidelines|phone|tel|check[- ]?in|营业|电话|官网|步行|地铁|捷运|预订)\b.*$",
    flags=re.IGNORECASE,
)


def _clean_text(value, limit: int = 600) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _metadata_value(metadata: dict, key: str, limit: int = 300) -> str:
    item = metadata.get(key) if isinstance(metadata, dict) else None
    value = item.get("value") if isinstance(item, dict) else item
    return _clean_text(value, limit)


def travel_search_queries(destination: str, year: int | None = None) -> dict[str, str]:
    """Return two provider-neutral searches; each can be handled as one credit."""
    place = _clean_text(destination, 48) or "destination"
    year_label = f" {year}" if isinstance(year, int) else ""
    return {
        "hotels": (
            f"{place}{year_label} recommended hotels official website street address "
            "neighborhood public transport"
        )[:320],
        "restaurants": (
            f"{place}{year_label} recommended restaurants official website street address "
            "local cuisine"
        )[:320],
    }


def _recommendation_name(title: str, kind: str, context: str = "") -> str:
    value = _clean_text(title, 180)
    value = re.sub(r"\s+[|｜]\s+.*$", "", value)
    value = re.sub(
        r"\s+[-–—]\s+(?:official(?:\s+site)?|官网|booking|tripadvisor|restaurant|hotel).*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"^\[?\d+\]?\s*", "", value).strip(" -–—|｜")
    lowered = value.lower()
    if any(marker in lowered for marker in _GENERIC_VENUE_MARKERS):
        return ""
    expected = ("hotel", "resort", "hostel", "inn", "酒店", "饭店", "宾馆", "旅馆") if kind == "hotels" else (
        "restaurant", "cafe", "bistro", "osteria", "trattoria", "餐厅", "餐馆", "酒楼", "咖啡",
    )
    if len(value) < 2 or len(value) > 90:
        return ""
    combined = f"{lowered} {_clean_text(context, 360).lower()}"
    if not any(marker in combined for marker in expected):
        return ""
    return value


def is_specific_venue_name(name: str, kind: str, context: str = "", destination: str = "") -> bool:
    value = _clean_text(name, 120).strip(" -–—|｜.,")
    lowered = value.casefold()
    if len(value) < 2 or len(value) > 90 or any(marker in lowered for marker in _GENERIC_VENUE_MARKERS):
        return False
    destination_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _clean_text(destination, 48).casefold())
    value_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", lowered)
    if destination_key and value_key == destination_key:
        return False
    venue_markers = (
        "hotel", "resort", "hostel", "inn", "lodge", "suites", "guesthouse", "hyatt", "hilton",
        "marriott", "酒店", "饭店", "宾馆", "旅馆", "客栈", "民宿",
    ) if kind == "hotels" else (
        "restaurant", "cafe", "café", "bistro", "osteria", "trattoria", "kitchen", "grill",
        "餐厅", "餐馆", "酒楼", "咖啡", "小馆", "食堂",
    )
    if any(marker in lowered for marker in venue_markers):
        return True
    nearby_context = _clean_text(context, 500).casefold()
    return bool(nearby_context and any(marker in nearby_context for marker in venue_markers))


def _normalize_venue_name(value: str) -> str:
    name = _clean_text(value, 110).strip(" -–—.,|｜")
    sentence_parts = re.split(r"[.!?] ?\s+(?=[A-ZÀ-ÖØ-Þ])", name)
    if len(sentence_parts) > 1:
        name = sentence_parts[-1]
    name = re.split(
        r"\s+(?:promo(?:tion)?\s+code|walking\s+distance|official\s+hotels?|nearby\s+hotels?|"
        r"official\s+restaurants?|nearby\s+restaurants?|website|phone|tel|address)\b",
        name,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return name.strip(" -–—.,|｜")[:90]


def _normalize_address(value: str) -> str:
    address = _clean_text(value, 180).strip(" ,，.-")
    address = _ADDRESS_STOP_PATTERN.sub("", address).strip(" ,，.-")
    address = re.split(r"\s+#{2,}\s*", address, maxsplit=1)[0].strip(" ,，.-")
    return address[:140]


def is_precise_venue_address(value: str) -> bool:
    address = _normalize_address(value)
    if len(address) < 7 or not re.search(r"\d", address):
        return False
    if re.search(r"https?://|walking\s+distance|minutes?\s+on\s+foot|\bmrt\b|\bmetro\b|book\s+now", address, re.IGNORECASE):
        return False
    if re.match(r"^\d{1,4}(?:-\d{1,4}){1,3}\s+", address) and re.search(
        r"\b(?:Ward|City|Prefecture|District|Tokyo|Kyoto|Osaka)\b", address, re.IGNORECASE
    ):
        return True
    address_markers = (
        r"\b(?:no\.?\s*)?\d+[A-Za-z-]*\s*,?\s*[A-Z][A-Za-z0-9 .'-]{1,80}\s"
        r"(?:St|Street|Rd|Road|Ave|Avenue|Blvd|Boulevard|Lane|Ln|Drive|Dr)\b",
        r"\b(?:Via|Viale|Piazza|Largo|Corso|Rue|Avenue|Avenida|Calle|Stra(?:ß|ss)e)\s+[^.;。；]{2,80}\d+[A-Za-z-]*",
        r"\bNo\.?\s*\d+[A-Za-z-]*\s*,\s*[^.;。；]{3,100}(?:Rd|Road|St|Street|Ave|Avenue|路|街|道)",
        r"\b\d{1,4}(?:-\d{1,4}){1,3}\s+[A-Z][A-Za-z0-9 .'-]{3,100}(?:Ward|City|Prefecture|District)\b",
        r"[\u4e00-\u9fff]{2,40}(?:路|街|道|巷|弄)\s*\d{1,6}\s*(?:号|號)",
        r"[\u4e00-\u9fff]{2,40}\d{1,4}(?:丁目|番地|番|号)",
    )
    return any(re.search(pattern, address, flags=re.IGNORECASE) for pattern in address_markers)


def _extract_address(snippet: str) -> str:
    text = _clean_text(snippet, 700)
    labelled = re.search(
        r"(?:(?:address|地址|所在地)\s*(?:is|为|是)?\s*[:：-]\s*|located at\s+)"
        r"([^。；;|\n]{5,120})",
        text,
        flags=re.IGNORECASE,
    )
    if labelled:
        value = _normalize_address(labelled.group(1))
        value = re.split(r"\.\s+(?=[A-Z\u4e00-\u9fff])", value, maxsplit=1)[0]
        return _normalize_address(value)
    patterns = (
        r"\b(?:Via|Viale|Piazza|Largo|Corso|Rue|Avenue|Avenida|Calle|Stra(?:ß|ss)e)\s+[^.;。；]{2,90}",
        r"\b\d{1,6}\s+[A-Z][A-Za-z0-9 .'-]{2,70}\s(?:St|Street|Rd|Road|Ave|Avenue|Blvd|Boulevard|Lane|Drive)\b[^.;。；]{0,35}",
        r"[\u4e00-\u9fff]{2,24}(?:路|街|道|巷|弄)\s*\d{1,6}\s*号[^。；;]{0,30}",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = re.split(r",\s+(?=[a-z])", match.group(0), maxsplit=1)[0]
            return _normalize_address(value)
    return ""


def _structured_venue_pairs(snippet: str, kind: str, destination: str) -> list[tuple[str, str, str]]:
    """Pair explicit venue headings with the adjacent address in list-page snippets."""
    text = _clean_text(snippet, 1100)
    pairs = []
    patterns = (
        re.compile(
            r"([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ0-9&'’ .-]{1,72}?"
            r"(?:Hotel(?:\s+and\s+Hostel)?|Hostel|Resort|Inn|Lodge|Suites)"
            r"(?:\s+[A-Z][A-Za-z0-9'’-]{1,20}){0,2})\s*:?[ ]*"
            r"(?:Check\s+Rates\s*)?(?:How\s+to\s+Get\s+There\s*:\s*)?[^#\n]{0,150}?"
            r"(?:address|地址)\s*(?:is|为|是)?\s*[:：-]\s*([^#\n]{7,180})",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"(?:#{2,}\s*)?([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ0-9&'’ .-]{2,70}?)\s+"
            r"(?:address|地址)\s*(?:is|为|是)?\s*[:：-]\s*([^#\n]{7,180})",
            flags=re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            if re.search(r"\[\s*\.{3}\s*\]|…", match.group(0)):
                continue
            name = _normalize_venue_name(match.group(1))
            address = _normalize_address(match.group(2))
            context = text[max(0, match.start() - 80):min(len(text), match.end() + 100)]
            if not is_specific_venue_name(name, kind, context, destination):
                continue
            if not is_precise_venue_address(address):
                continue
            key = (name.casefold(), address.casefold())
            if key not in {(item[0].casefold(), item[1].casefold()) for item in pairs}:
                pairs.append((name, address, context))
    return pairs[:4]


def _snippet_candidate_names(snippet: str, kind: str) -> list[str]:
    """Extract venue names from list-article snippets without treating the article as a venue."""
    text = _clean_text(snippet, 700)
    patterns = (
        r"(?:^|[.!?]\s+)([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ0-9&'’ .-]{1,48}?)\s+(?:is|offers|sits|lies|features)\b",
        r"(?:^|[.;]\s+)([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ0-9&'’ .-]{1,48}?)\s*[–—-]\s*(?:located|tucked|set|found|a\b)",
        r"\b(?:first|second|another)\s+(?:one\s+)?(?:is|:)?\s*([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ0-9&'’ .-]{1,44})",
        r"\b(?:service|dinner|lunch|stay)\s+at\s+([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ0-9&'’ .-]{1,36}?)\s+(?:is|was|offers)\b",
        r"(?:^|[。；]\s*)([\u4e00-\u9fffA-Za-z0-9·&'’ ]{2,32}?)(?:是一家|位于|坐落于|餐厅位于|酒店位于)",
    )
    stop_phrases = (
        "the hotel", "the restaurant", "the service", "the neighborhood", "a recent", "read full", "image ",
        "this hotel", "this restaurant", "the first", "the second", "official guide",
    )
    names = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            name = _clean_text(match.group(1), 70).strip(" -–—.,")
            lowered = name.casefold()
            if not name or any(phrase in lowered for phrase in stop_phrases):
                continue
            if len(name.split()) > 7 or name.casefold() in {item.casefold() for item in names}:
                continue
            names.append(name)
            if len(names) >= 4:
                return names
    return names


def recommendations_from_citations(
    destination: str,
    kind: str,
    citations: list[dict] | None,
    limit: int = MAX_TRAVEL_RECOMMENDATIONS,
) -> list[dict]:
    """Reduce search results to transparent, map-verifiable venue candidates."""
    safe_kind = "restaurants" if kind == "restaurants" else "hotels"
    results = []
    seen = set()
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        source_url = str(citation.get("url") or "").strip()
        if not source_url.startswith(("https://", "http://")):
            continue
        snippet = _clean_text(citation.get("snippet"), 360)
        direct_name = _recommendation_name(citation.get("title") or "", safe_kind, snippet)
        structured_pairs = _structured_venue_pairs(citation.get("snippet") or "", safe_kind, destination)
        names = [direct_name] if direct_name else _snippet_candidate_names(snippet, safe_kind)
        candidates = list(structured_pairs)
        candidates.extend((name, "", "") for name in names)
        for name, paired_address, paired_context in candidates:
            if not name or name.casefold() in seen:
                continue
            name_position = snippet.casefold().find(name.casefold())
            context = paired_context or (snippet[max(0, name_position - 60):name_position + 340] if name_position >= 0 else snippet)
            if not is_specific_venue_name(name, safe_kind, context, destination):
                continue
            address = paired_address or _extract_address(context)
            if not is_precise_venue_address(address):
                continue
            address = _normalize_address(address)
            source_title = _clean_text(citation.get("title"), 180) or _clean_text(citation.get("domain"), 180) or "公开网页"
            venue_label = "酒店" if safe_kind == "hotels" else "餐厅"
            seen.add(name.casefold())
            map_query = " ".join(part for part in (name, address, destination) if part)
            results.append(
                {
                    "name": name,
                    "address": address,
                    "address_verified": True,
                    "summary": f"公开来源「{source_title}」同时列出该{venue_label}名称与上述门牌地址；预订前请通过来源和地图复核。",
                    "source_title": source_title,
                    "source_url": source_url,
                    "domain": _clean_text(citation.get("domain"), 180),
                    "map_url": f"https://www.google.com/maps/search/?api=1&query={quote_plus(map_query)}",
                    "category": safe_kind,
                }
            )
            if len(results) >= max(1, min(int(limit), MAX_TRAVEL_RECOMMENDATIONS)):
                return results
    return results


def append_recommendations_to_answer(answer: str, guide: dict) -> str:
    """Add useful candidates to chat even when the model omits venue details."""
    sections = []
    for key, heading in (("hotels", "住宿候选"), ("restaurants", "餐饮候选")):
        items = guide.get(key) if isinstance(guide.get(key), list) else []
        if not items:
            venue_label = "酒店" if key == "hotels" else "餐厅"
            sections.append(
                f"**{heading}**\n本次未找到同时满足“明确{venue_label}名称 + 可核验街道门牌”的候选，"
                "因此不展示榜单页面、地区名称或仅有坐标的模糊点位。"
            )
            continue
        lines = [f"**{heading}（预订前核实）**"]
        for index, item in enumerate(items[:4], start=1):
            lines.append(
                f"{index}. {item['name']}｜{item['address']}｜地图：{item['map_url']}"
            )
        sections.append("\n".join(lines))
    if not sections:
        return str(answer or "").strip()
    notice = "酒店、餐厅与地址来自公开网页候选，不代表商业背书；请通过来源和地图核实营业状态、评价及预订条件。"
    return f"{str(answer or '').rstrip()}\n\n" + "\n\n".join(sections) + f"\n\n{notice}"


class WikimediaImageClient:
    """Small allowlisted Wikimedia Commons media adapter with attribution."""

    def __init__(self, fetcher: Callable | None = None, timeout_seconds: int = 8):
        self.timeout_seconds = max(1, min(int(timeout_seconds), 10))
        self._fetcher = fetcher or self._urlopen
        self._cache: dict[str, list[dict]] = {}

    @staticmethod
    def _urlopen(req: request.Request, timeout: int) -> bytes:
        with request.urlopen(req, timeout=timeout) as response:
            return response.read()

    @staticmethod
    def _safe_media_url(value: str) -> str:
        raw = str(value or "").strip()
        try:
            parsed = urlparse(raw)
        except ValueError:
            return ""
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host.endswith(".wikimedia.org"):
            return ""
        return raw

    def search(self, destination: str, limit: int = MAX_TRAVEL_IMAGES, search_label: str | None = None) -> list[dict]:
        place = _clean_text(destination, 48)
        if not place or place == "目的地":
            return []
        safe_limit = max(1, min(int(limit), MAX_TRAVEL_IMAGES))
        lookup_label = _clean_text(search_label, 80) or place
        cache_key = f"{place.casefold()}:{lookup_label.casefold()}"
        if cache_key in self._cache:
            return [dict(item) for item in self._cache[cache_key][:safe_limit]]
        images = []
        seen = set()
        queries = (
            (f"{lookup_label} landmark cityscape", "city", 2),
            (f"{lookup_label} local cuisine", "food", 1),
            (f"{lookup_label} travel", "city", 1),
        )
        for query_text, kind, quota in queries:
            params = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "generator": "search",
                "gsrsearch": query_text,
                "gsrnamespace": "6",
                "gsrlimit": "8",
                "prop": "imageinfo",
                "iiprop": "url|mime|extmetadata",
                "iiurlwidth": "1000",
                "origin": "*",
            }
            req = request.Request(
                f"{WIKIMEDIA_API}?{urlencode(params)}",
                method="GET",
                headers={"Accept": "application/json", "User-Agent": "DeepVisionTravelGuide/1.0"},
            )
            try:
                payload = json.loads(self._fetcher(req, self.timeout_seconds).decode("utf-8"))
            except (error.HTTPError, error.URLError, TimeoutError, OSError, ValueError, TypeError, UnicodeDecodeError):
                continue
            pages = (payload.get("query") or {}).get("pages") if isinstance(payload, dict) else []
            pages = pages if isinstance(pages, list) else []
            added = 0
            for page in pages:
                info_rows = page.get("imageinfo") if isinstance(page, dict) else []
                info = info_rows[0] if isinstance(info_rows, list) and info_rows and isinstance(info_rows[0], dict) else {}
                mime_type = str(info.get("mime") or "").lower()
                thumbnail_url = self._safe_media_url(info.get("thumburl") or info.get("url"))
                original_url = self._safe_media_url(info.get("url"))
                if mime_type not in _IMAGE_MIME_TYPES or not thumbnail_url or thumbnail_url in seen:
                    continue
                metadata = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}
                license_name = _metadata_value(metadata, "LicenseShortName", 80)
                if not license_name:
                    continue
                seen.add(thumbnail_url)
                page_id = page.get("pageid")
                title = _clean_text(page.get("title"), 160).removeprefix("File:")
                title = re.sub(r"\.(?:jpe?g|png|webp)$", "", title, flags=re.IGNORECASE)
                description = _metadata_value(metadata, "ImageDescription", 180)
                if "provided description" in description.casefold() or len(description) > 120:
                    description = ""
                images.append(
                    {
                        "title": description or title or place,
                        "thumbnail_url": thumbnail_url,
                        "original_url": original_url or thumbnail_url,
                        "mime_type": mime_type,
                        "author": _metadata_value(metadata, "Artist", 160) or "Wikimedia Commons contributor",
                        "license": license_name,
                        "license_url": self._safe_media_url(_metadata_value(metadata, "LicenseUrl", 500)),
                        "source_url": f"https://commons.wikimedia.org/?curid={page_id}" if page_id else "https://commons.wikimedia.org/",
                        "kind": kind,
                    }
                )
                added += 1
                if added >= quota or len(images) >= safe_limit:
                    break
            if len(images) >= safe_limit:
                break
        self._cache[cache_key] = [dict(item) for item in images]
        return images[:safe_limit]

    def download(self, image: dict) -> bytes | None:
        url = self._safe_media_url((image or {}).get("thumbnail_url"))
        mime_type = str((image or {}).get("mime_type") or "").lower()
        if not url or mime_type not in _IMAGE_MIME_TYPES:
            return None
        req = request.Request(
            url,
            method="GET",
            headers={"Accept": mime_type, "User-Agent": "DeepVisionTravelGuide/1.0"},
        )
        raw = None
        for _attempt in range(2):
            try:
                raw = self._fetcher(req, self.timeout_seconds)
                break
            except (error.HTTPError, error.URLError, TimeoutError, OSError):
                continue
        if not isinstance(raw, (bytes, bytearray)) or not raw or len(raw) > MAX_IMAGE_BYTES:
            return None
        return bytes(raw)


class WikidataTravelClient:
    """Resolve destinations and addressable place candidates from open data."""

    _VALID_DESCRIPTIONS = {
        "hotels": ("hotel", "guesthouse", "hostel", "inn", "resort", "accommodation", "酒店", "旅馆", "住宿"),
        "restaurants": ("restaurant", "eatery", "cafe", "café", "餐厅", "餐馆", "饭店"),
    }

    def __init__(self, fetcher: Callable | None = None, timeout_seconds: int = 10):
        self.timeout_seconds = max(2, min(int(timeout_seconds), 15))
        self._fetcher = fetcher or self._urlopen
        self._destination_cache: dict[str, dict] = {}
        self._places_cache: dict[str, dict[str, list[dict]]] = {}

    @staticmethod
    def _urlopen(req: request.Request, timeout: int) -> bytes:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Wikidata response too large")
        return raw

    def _json(self, url: str) -> dict:
        req = request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "DeepVisionTravelGuide/1.0"},
        )
        raw = self._fetcher(req, self.timeout_seconds)
        if not isinstance(raw, (bytes, bytearray)):
            raise ValueError("Wikidata response must be bytes")
        parsed = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Wikidata response must be an object")
        return parsed

    def resolve_destination(self, destination: str) -> dict:
        place = _clean_text(destination, 48)
        if not place or place == "目的地":
            return {}
        cache_key = place.casefold()
        if cache_key in self._destination_cache:
            return dict(self._destination_cache[cache_key])
        search_query = urlencode({
            'action': 'wbsearchentities', 'search': place, 'language': 'zh',
            'uselang': 'en', 'limit': '1', 'format': 'json',
        })
        search_url = f"{WIKIDATA_API}?{search_query}"
        try:
            search_payload = self._json(search_url)
            rows = search_payload.get("search") if isinstance(search_payload.get("search"), list) else []
            match = rows[0] if rows and isinstance(rows[0], dict) else {}
            entity_id = str(match.get("id") or "")
            if not re.fullmatch(r"Q\d+", entity_id):
                return {}
            entity_query = urlencode({
                'action': 'wbgetentities', 'ids': entity_id, 'props': 'claims|labels',
                'languages': 'zh|en', 'format': 'json',
            })
            entity_url = f"{WIKIDATA_API}?{entity_query}"
            entity_payload = self._json(entity_url)
            entity = (entity_payload.get("entities") or {}).get(entity_id) or {}
            claims = entity.get("claims") if isinstance(entity.get("claims"), dict) else {}
            labels = entity.get("labels") if isinstance(entity.get("labels"), dict) else {}
            coordinate_claims = claims.get("P625") if isinstance(claims.get("P625"), list) else []
            data_value = (((coordinate_claims[0] if coordinate_claims else {}).get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if not isinstance(data_value, dict):
                return {}
            latitude = float(data_value.get("latitude"))
            longitude = float(data_value.get("longitude"))
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                return {}
        except (error.HTTPError, error.URLError, TimeoutError, OSError, ValueError, TypeError, UnicodeDecodeError):
            return {}
        match_label = _clean_text(match.get("label"), 80)
        alternate_labels = []
        for language in ("zh", "en"):
            label_data = labels.get(language) if isinstance(labels.get(language), dict) else {}
            alternate = _clean_text(label_data.get("value"), 80)
            known = {place.casefold(), match_label.casefold(), *(item.casefold() for item in alternate_labels)}
            if alternate and alternate.casefold() not in known:
                alternate_labels.append(alternate)
        result = {
            "entity_id": entity_id,
            "label": match_label or place,
            "aliases": alternate_labels,
            "description": _clean_text(match.get("description"), 180),
            "latitude": latitude,
            "longitude": longitude,
            "source_url": f"https://www.wikidata.org/wiki/{entity_id}",
        }
        self._destination_cache[cache_key] = dict(result)
        return result

    @staticmethod
    def _binding_value(binding: dict, key: str) -> str:
        value = binding.get(key) if isinstance(binding, dict) else None
        return _clean_text(value.get("value") if isinstance(value, dict) else "", 500)

    @staticmethod
    def _coordinates(value: str) -> tuple[float | None, float | None]:
        match = re.fullmatch(r"Point\((-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\)", value)
        if not match:
            return None, None
        return float(match.group(2)), float(match.group(1))

    @staticmethod
    def _mentioned_source(name: str, citations: list[dict]) -> dict | None:
        needle = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", name.casefold()).strip()
        if len(needle) < 3:
            return None
        for item in citations or []:
            corpus = re.sub(
                r"[^a-z0-9\u4e00-\u9fff]+",
                " ",
                f"{item.get('title') or ''} {item.get('snippet') or ''}".casefold(),
            )
            if needle in corpus:
                return item
        return None

    def recommendations(
        self,
        destination: str,
        destination_info: dict,
        citation_groups: dict[str, list[dict]],
        limit: int = MAX_TRAVEL_RECOMMENDATIONS,
    ) -> dict[str, list[dict]]:
        entity_id = str((destination_info or {}).get("entity_id") or "")
        if not re.fullmatch(r"Q\d+", entity_id):
            return {"hotels": [], "restaurants": []}
        if entity_id in self._places_cache:
            return {key: [dict(item) for item in value] for key, value in self._places_cache[entity_id].items()}
        latitude = float(destination_info["latitude"])
        longitude = float(destination_info["longitude"])
        query_text = f'''SELECT DISTINCT ?category ?place ?placeLabel ?placeDescription ?coord ?address ?website ?streetLabel ?streetNumber WHERE {{
          SERVICE wikibase:around {{
            ?place wdt:P625 ?coord .
            bd:serviceParam wikibase:center "Point({longitude} {latitude})"^^geo:wktLiteral .
            bd:serviceParam wikibase:radius "8" .
          }}
          {{ ?place wdt:P31/wdt:P279* wd:Q27686 . BIND("hotels" AS ?category) }}
          UNION
          {{ ?place wdt:P31 wd:Q11707 . BIND("restaurants" AS ?category) }}
          OPTIONAL {{ ?place wdt:P6375 ?address. }}
          OPTIONAL {{ ?place wdt:P856 ?website. }}
          OPTIONAL {{ ?place wdt:P669 ?street. OPTIONAL {{ ?place wdt:P670 ?streetNumber. }} }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,zh". }}
        }} LIMIT 180'''
        sparql_url = f"{WIKIDATA_SPARQL}?{urlencode({'query': query_text, 'format': 'json'})}"
        try:
            payload = self._json(sparql_url)
            bindings = (((payload.get("results") or {}).get("bindings")) or [])
        except (error.HTTPError, error.URLError, TimeoutError, OSError, ValueError, TypeError, UnicodeDecodeError):
            return {"hotels": [], "restaurants": []}
        candidates = {"hotels": {}, "restaurants": {}}
        for binding in bindings if isinstance(bindings, list) else []:
            category = self._binding_value(binding, "category")
            if category not in candidates:
                continue
            entity_url = self._binding_value(binding, "place")
            entity_match = re.search(r"/(Q\d+)$", entity_url)
            name = self._binding_value(binding, "placeLabel")
            description = self._binding_value(binding, "placeDescription")
            lowered_description = description.casefold()
            valid_terms = self._VALID_DESCRIPTIONS[category]
            mentioned = self._mentioned_source(name, citation_groups.get(category) or [])
            if not any(term in lowered_description for term in valid_terms) and not mentioned:
                continue
            invalid_name_terms = {
                "restaurants": ("water tower", "church", "basilica", "museum", "airport", "railway station", "monument", "temple"),
                "hotels": ("water tower", "church", "basilica", "museum", "airport", "railway station"),
            }
            lowered_name = name.casefold()
            if not mentioned and any(term in lowered_name for term in invalid_name_terms[category]):
                continue
            latitude_value, longitude_value = self._coordinates(self._binding_value(binding, "coord"))
            if not name or not entity_match or latitude_value is None or longitude_value is None:
                continue
            address = self._binding_value(binding, "address")
            if not address:
                street = self._binding_value(binding, "streetLabel")
                street_number = self._binding_value(binding, "streetNumber")
                address = " ".join(part for part in (street, street_number) if part)
            address = _normalize_address(address)
            address_verified = is_precise_venue_address(address)
            place_id = entity_match.group(1)
            existing = candidates[category].get(place_id)
            if existing and existing.get("address_verified"):
                continue
            exact_map = f"{latitude_value:.6f},{longitude_value:.6f}"
            source = mentioned or {}
            item = {
                "name": name,
                "address": address if address_verified else f"{_clean_text(destination, 48)} · 坐标 {exact_map}",
                "address_verified": address_verified,
                "summary": description or f"{_clean_text(destination, 48)}的公开地点候选。",
                "source_title": _clean_text(source.get("title"), 180) or "Wikidata 公开地点数据",
                "source_url": str(source.get("url") or f"https://www.wikidata.org/wiki/{place_id}"),
                "place_data_url": f"https://www.wikidata.org/wiki/{place_id}",
                "map_url": f"https://www.google.com/maps/search/?api=1&query={quote_plus(exact_map)}",
                "category": category,
                "editorial_match": bool(mentioned),
                "score": (20 if mentioned else 0) + (6 if address_verified else 0) + (2 if self._binding_value(binding, "website") else 0),
            }
            candidates[category][place_id] = item
        result = {}
        for category, items in candidates.items():
            ranked = sorted(
                items.values(),
                key=lambda item: (-item["score"], not item["address_verified"], item["name"].casefold()),
            )
            result[category] = [{key: value for key, value in item.items() if key != "score"} for item in ranked[:limit]]
        self._places_cache[entity_id] = {key: [dict(item) for item in value] for key, value in result.items()}
        return result


def travel_guide_payload(
    destination: str,
    days: int,
    year: int | None,
    hotels: list[dict] | None = None,
    restaurants: list[dict] | None = None,
    images: list[dict] | None = None,
    destination_info: dict | None = None,
) -> dict:
    return {
        "destination": _clean_text(destination, 48) or "目的地",
        "days": max(1, min(int(days or 5), 14)),
        "travel_year": year if isinstance(year, int) else None,
        "hotels": list(hotels or [])[:MAX_TRAVEL_RECOMMENDATIONS],
        "restaurants": list(restaurants or [])[:MAX_TRAVEL_RECOMMENDATIONS],
        "images": list(images or [])[:MAX_TRAVEL_IMAGES],
        "destination_info": dict(destination_info or {}),
        "places_provider": "Wikidata" if destination_info else None,
        "places_attribution_url": "https://www.wikidata.org/wiki/Wikidata:Licensing" if destination_info else None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recommendation_notice": (
            "地点来自公开网页候选，不代表商业背书；地址、评价、营业状态、价格和预订条件须在出发前复核。"
        ),
    }
