"""Best-effort DOI title verification without uploading document content."""

from __future__ import annotations

import asyncio
import html
import json
import re
import unicodedata
from collections import OrderedDict
from dataclasses import replace
from html.parser import HTMLParser
from time import monotonic
from urllib.parse import quote

import aiohttp

from .documents import DocumentInfo

LOOKUP_TIMEOUT_SECONDS = 6
PROVIDER_TIMEOUT_SECONDS = 3
MAX_METADATA_BYTES = 512 * 1024
MAX_CACHE_ENTRIES = 256
CACHE_TTL_SECONDS = 6 * 60 * 60
TITLE_PREFIX_CHARACTERS = 3000
_PROVIDERS = (
    ("crossref", "https://api.crossref.org/works/"),
    ("datacite", "https://api.datacite.org/dois/"),
)
_DOI = re.compile(r"10\.\d{4,9}/\S+")
_SECTION = re.compile(
    r"^[ \t]*(?:\d+(?:\.\d+)*[.)]?[ \t]+)?"
    r"(?:abstract|introduction|references|bibliography|초록|서론|참고[ \t]*문헌)\b",
    re.IGNORECASE | re.MULTILINE,
)
_JSON_TYPE = re.compile(r"application/(?:[a-z0-9.+-]+\+)?json")


class _PlainTitle(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _clean_title(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw or len(raw) > 8192:
        return None
    parser = _PlainTitle()
    parser.feed(html.unescape(raw))
    parser.close()
    text = html.unescape("".join(parser.parts))
    text = " ".join(
        "".join(
            " " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char for char in text
        ).split()
    )
    return text if text and len(text) <= 512 else None


def _compact(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum()
    )


def _title_in_front_matter(text: str, title: str) -> bool:
    """Require a title-shaped block before prose/citations, not a cited phrase."""
    target = _compact(title)
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", title), re.UNICODE)
    cjk = len(re.findall(r"[\u3400-\u9fff\uac00-\ud7af]", title))
    short = len(tokens) <= 2 or len(target) < 24
    if len(tokens) == 1 and cjk < 10:
        return False
    if short and len(target) < (10 if cjk >= 10 else 24):
        return False
    prefix = text[:TITLE_PREFIX_CHARACTERS]
    lines = []
    offset = 0
    for line in prefix.splitlines(keepends=True):
        stripped = re.sub(r"^[ \t]*title[ \t]*[:：][ \t]*", "", line, flags=re.IGNORECASE)
        compact = _compact(stripped)
        if compact:
            lines.append((offset, compact))
        offset += len(line)
    for index, (position, _) in enumerate(lines):
        if short and position > 1000:
            break
        combined = ""
        for _, compact in lines[index : index + (2 if short else 6)]:
            combined += compact
            if len(combined) > len(target):
                break
            if combined == target:
                # Even an exact cited title after an inline References heading
                # or Introduction must never replace the uploaded paper title.
                return _SECTION.search(prefix[:position]) is None
    return False


def _valid_doi(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 255
        or not _DOI.fullmatch(value)
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        return None
    return value.casefold()


def _official_title(payload: object, provider: str, doi: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    if provider == "crossref":
        work = payload.get("message")
        if payload.get("status") != "ok" or not isinstance(work, dict):
            return None
        if not isinstance(work.get("DOI"), str) or work["DOI"].casefold() != doi:
            return None
        titles = work.get("title")
        if not isinstance(titles, list):
            return None
        candidates = titles[:8]
    else:
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("type") != "dois":
            return None
        attributes = data.get("attributes")
        if not isinstance(attributes, dict):
            return None
        if (
            not isinstance(data.get("id"), str)
            or data["id"].casefold() != doi
            or not isinstance(attributes.get("doi"), str)
            or attributes["doi"].casefold() != doi
        ):
            return None
        titles = attributes.get("titles")
        if not isinstance(titles, list):
            return None
        candidates = [
            item.get("title")
            for item in titles[:8]
            if isinstance(item, dict) and item.get("titleType") in (None, "MainTitle")
        ]
    for candidate in candidates:
        title = _clean_title(candidate)
        if title:
            return title
    return None


async def _fetch_title(
    session: aiohttp.ClientSession, provider: str, endpoint: str, doi: str
) -> str | None:
    async with asyncio.timeout(PROVIDER_TIMEOUT_SECONDS):
        async with session.get(
            endpoint + quote(doi, safe=""),
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=PROVIDER_TIMEOUT_SECONDS),
        ) as response:
            if response.status != 200:
                return None
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if not _JSON_TYPE.fullmatch(content_type):
                return None
            if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                return None
            if response.content_length is not None and response.content_length > MAX_METADATA_BYTES:
                return None
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES:
                    return None
            return _official_title(json.loads(body.decode("utf-8")), provider, doi)


class PaperMetadataResolver:
    """Cache public DOI titles in memory and verify each document separately."""

    def __init__(self):
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def enrich(self, info: DocumentInfo) -> DocumentInfo:
        doi = _valid_doi(info.doi)
        if doi is None or not isinstance(info.text, str) or not info.text.strip():
            return info
        cached = self._cache.get(doi)
        if cached is not None and cached[0] > monotonic():
            self._cache.move_to_end(doi)
            title = cached[1]
        else:
            self._cache.pop(doi, None)
            try:
                async with asyncio.timeout(LOOKUP_TIMEOUT_SECONDS):
                    title = await self._lookup(doi)
            except Exception:
                # Best-effort metadata must not break uploads. Cancellation is
                # deliberately not caught, and no raw network error is logged.
                return info
            if not title:
                return info
            self._cache[doi] = (monotonic() + CACHE_TTL_SECONDS, title)
            self._cache.move_to_end(doi)
            while len(self._cache) > MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)
        if title != info.title and _title_in_front_matter(info.text, title):
            return replace(info, title=title)
        return info

    async def _lookup(self, doi: str) -> str | None:
        async with aiohttp.ClientSession(
            trust_env=False,
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=PROVIDER_TIMEOUT_SECONDS),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "LabSeminarBot/1.0",
            },
        ) as session:
            for provider, endpoint in _PROVIDERS:
                try:
                    title = await _fetch_title(session, provider, endpoint, doi)
                except (aiohttp.ClientError, OSError, TimeoutError, ValueError, RecursionError):
                    continue
                if title:
                    return title
        return None
