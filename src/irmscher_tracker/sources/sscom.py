"""SS.com RSS discovery and public listing-page enrichment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
import yaml  # type: ignore[import-untyped]
from defusedxml import ElementTree  # type: ignore[import-untyped]
from pydantic import BaseModel, HttpUrl

from irmscher_tracker.domain import (
    ListingCondition,
    NormalizedListing,
    SearchHit,
    Source,
    SourceSearchResult,
)
from irmscher_tracker.sources.base import SourceAdapter

USER_AGENT = "SignumPartTracker/0.1 (personal-use RSS reader)"
FETCH_HOST = "www.ss.com"
IMAGE_HOST = "i.ss.com"
FEED_LIMIT = 2 * 1024 * 1024
DETAIL_LIMIT = 5 * 1024 * 1024
METADATA_TEXT_LIMIT = 8192
DESCRIPTION_LIMIT = 65536


class SscomConfig(BaseModel):
    feeds: list[HttpUrl]


class SourcesConfig(BaseModel):
    sscom: SscomConfig


def load_feed_urls(path: str | Path) -> list[str]:
    with Path(path).open(encoding="utf-8") as config_file:
        config = SourcesConfig.model_validate(yaml.safe_load(config_file))
    urls = [_validate_url(str(url)) for url in config.sscom.feeds]
    for url in urls:
        _feed_model(url)
    return urls


class SscomError(ValueError):
    """Sanitized SS.com retrieval or parsing failure."""


@dataclass
class _FeedCandidate:
    external_id: str
    title: str
    summary: str
    url: str
    published_at: datetime | None
    thumbnail: str | None
    fingerprint: str
    feeds: set[str] = field(default_factory=set)


@dataclass
class _DetailData:
    description: str
    price: Decimal | None
    location: str
    published_at: datetime | None
    condition: ListingCondition
    model: str
    year: str
    category: str
    image_urls: list[str]
    canonical_url: str


class _SummaryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.thumbnail: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "img" and self.thumbnail is None:
            self.thumbnail = attributes.get("src")
        if tag == "br":
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        self.text.append(data)


class SscomDetailParser(HTMLParser):
    """Extract only public listing fields from SS.com's server-rendered HTML."""

    _LABELS: ClassVar[dict[str, str]] = {
        "marka": "model",
        "марка": "model",
        "izlaiduma gads": "year",
        "год выпуска": "year",
        "tips": "category",
        "тип": "category",
        "stāvoklis": "condition",
        "stāv.": "condition",
        "состояние": "condition",
        "сост.": "condition",
        "vieta": "location",
        "место": "location",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.description_parts: list[str] = []
        self.fields: dict[str, str] = {}
        self.price_text = ""
        self.image_urls: list[str] = []
        self.canonical_url: str | None = None
        self.date_text: str | None = None
        self.saw_message = False
        self._in_message = False
        self._message_depth = 0
        self._description_done = False
        self._in_row = False
        self._cell_id = ""
        self._cell_text: list[str] | None = None
        self._row_cells: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div" and attributes.get("id") == "msg_div_msg":
            self.saw_message = True
            self._in_message = True
            self._message_depth = 1
        elif self._in_message and tag == "div":
            self._message_depth += 1

        if self._in_message and tag == "table" and not self._description_done:
            self._description_done = True
        if tag == "tr":
            self._in_row = True
            self._row_cells = []
        elif self._in_row and tag == "td":
            self._cell_id = attributes.get("id", "") or ""
            self._cell_text = []
        elif tag == "br" and self._in_message and not self._description_done:
            self.description_parts.append("\n")

        if tag == "a":
            href = attributes.get("href")
            if href and _is_image_url(href) and href not in self.image_urls:
                self.image_urls.append(href)
        elif tag == "link" and (attributes.get("rel") or "").lower() == "canonical":
            self.canonical_url = attributes.get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._cell_text is not None:
            text = _clean_text(" ".join(self._cell_text))
            self._row_cells.append((self._cell_id, text))
            self._cell_text = None
            self._cell_id = ""
        elif tag == "tr" and self._in_row:
            self._process_row()
            self._in_row = False
        if self._in_message and tag == "div":
            self._message_depth -= 1
            if self._message_depth == 0:
                self._in_message = False

    def handle_data(self, data: str) -> None:
        if self._in_message and not self._description_done:
            self.description_parts.append(data)
        if self._cell_text is not None:
            self._cell_text.append(data)
        cleaned = _clean_text(data)
        if cleaned.lower().startswith(("datums:", "дата:")):
            self.date_text = cleaned.split(":", 1)[1].strip()

    def _process_row(self) -> None:
        for cell_id, text in self._row_cells:
            if cell_id == "tdo_8":
                self.price_text = text
        if len(self._row_cells) < 2:
            return
        label = self._row_cells[0][1].strip().rstrip(":").lower()
        field_name = self._LABELS.get(label)
        if field_name:
            self.fields[field_name] = self._row_cells[1][1]


class SscomAdapter(SourceAdapter):
    def __init__(
        self,
        feed_urls: list[str],
        known_listings: dict[str, NormalizedListing] | None = None,
        timeout: float = 20.0,
        max_detail_requests: int = 30,
        detail_refresh_hours: int = 24,
        request_delay: float = 0.5,
        retry_base_delay: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._feed_urls = feed_urls
        self._known = known_listings or {}
        self._max_detail_requests = max_detail_requests
        self._refresh_age = timedelta(hours=detail_refresh_hours)
        self._request_delay = request_delay
        self._retry_base_delay = retry_base_delay
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._pace_lock = asyncio.Lock()
        self._last_request_at = 0.0

    @property
    def source_name(self) -> Source:
        return Source.SSCOM

    async def search(self, queries: list[str]) -> SourceSearchResult:
        del queries
        candidates: dict[str, _FeedCandidate] = {}
        successful_feeds: list[str] = []
        errors: dict[str, str] = {}

        for feed_url in self._feed_urls:
            try:
                xml, final_url = await self._fetch(
                    feed_url,
                    {"text/xml", "application/xml", "application/rss+xml"},
                    FEED_LIMIT,
                    feed=True,
                )
                for candidate in self._parse_feed(xml, final_url):
                    existing = candidates.get(candidate.external_id)
                    if existing is None:
                        candidates[candidate.external_id] = candidate
                    else:
                        existing.feeds.update(candidate.feeds)
                successful_feeds.append(feed_url)
            except Exception as exc:
                errors[feed_url] = _error_name(exc)

        ordered = sorted(candidates.values(), key=self._priority)
        detail_candidates = [candidate for candidate in ordered if self._needs_detail(candidate)]
        allowed = {
            candidate.external_id for candidate in detail_candidates[: self._max_detail_requests]
        }
        enrichment_complete = True
        semaphore = asyncio.Semaphore(2)

        async def build(candidate: _FeedCandidate) -> SearchHit:
            nonlocal enrichment_complete
            listing = self._rss_listing(candidate)
            if not self._needs_detail(candidate):
                return SearchHit(listing=listing, queries=candidate.feeds)
            if candidate.external_id not in allowed:
                enrichment_complete = False
                listing.detail_status = "deferred"
                errors[candidate.url] = "Detail request budget exhausted"
                return SearchHit(listing=listing, queries=candidate.feeds)
            try:
                async with semaphore:
                    html, final_url = await self._fetch(
                        candidate.url,
                        {"text/html", "application/xhtml+xml"},
                        DETAIL_LIMIT,
                        listing=True,
                    )
                detail = self._parse_detail(html.decode("utf-8", errors="replace"), final_url)
                listing = self._apply_detail(listing, detail, candidate.fingerprint)
            except Exception as exc:
                enrichment_complete = False
                listing.detail_status = "failed"
                errors[candidate.url] = _error_name(exc)
            return SearchHit(listing=listing, queries=candidate.feeds)

        hits = await asyncio.gather(*(build(candidate) for candidate in ordered))
        return SourceSearchResult(
            hits=list(hits),
            successful_queries=successful_feeds,
            query_errors=errors,
            discovery_complete=len(successful_feeds) == len(self._feed_urls),
            enrichment_complete=enrichment_complete,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _parse_feed(self, content: bytes, feed_url: str) -> list[_FeedCandidate]:
        try:
            root = ElementTree.fromstring(content)
        except Exception as exc:
            raise SscomError("Malformed RSS XML") from exc
        if root.tag != "rss" or root.find("channel") is None:
            raise SscomError("Malformed RSS document")
        expected_model = _feed_model(feed_url)
        candidates: list[_FeedCandidate] = []
        for item in root.findall("./channel/item"):
            link = _clean_text(item.findtext("link") or "")
            try:
                url = _validate_url(urljoin(feed_url, link), listing=True)
                if _listing_model(url) != expected_model:
                    continue
                external_id = _external_id(url)
            except SscomError:
                continue
            title = _clean_text(item.findtext("title") or "")
            summary_html = item.findtext("description") or ""
            summary_parser = _SummaryParser()
            summary_parser.feed(summary_html)
            summary = _clean_text(" ".join(summary_parser.text))[:METADATA_TEXT_LIMIT]
            published_at = _parse_rss_date(item.findtext("pubDate"))
            thumbnail = summary_parser.thumbnail
            if thumbnail and not _is_image_url(thumbnail):
                thumbnail = None
            fingerprint_payload = json.dumps(
                [
                    title,
                    summary,
                    published_at.isoformat() if published_at else None,
                    url,
                    thumbnail,
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            candidates.append(
                _FeedCandidate(
                    external_id=external_id,
                    title=title,
                    summary=summary,
                    url=url,
                    published_at=published_at,
                    thumbnail=thumbnail,
                    fingerprint=hashlib.sha256(fingerprint_payload.encode()).hexdigest(),
                    feeds={feed_url},
                )
            )
        return candidates

    def _rss_listing(self, candidate: _FeedCandidate) -> NormalizedListing:
        known = self._known.get(candidate.external_id)
        metadata = {
            "schema_version": 1,
            "feed_urls": sorted(candidate.feeds),
            "rss_summary": candidate.summary[:METADATA_TEXT_LIMIT],
            "model": _listing_model(candidate.url),
            "year": "",
            "category": "",
        }
        if known is not None:
            listing = known.model_copy(deep=True)
            listing.title = candidate.title
            listing.url = candidate.url
            listing.published_at = candidate.published_at or listing.published_at
            listing.source_metadata = {**listing.source_metadata, **metadata}
            listing.raw_data = listing.source_metadata.copy()
            listing.rss_fingerprint_seen = candidate.fingerprint
            return listing
        listing = NormalizedListing(
            source=Source.SSCOM,
            external_id=candidate.external_id,
            title=candidate.title,
            description=candidate.summary,
            url=candidate.url,
            image_urls=[candidate.thumbnail] if candidate.thumbnail else [],
            price=_parse_price(candidate.summary),
            currency="EUR",
            condition=_map_condition(candidate.summary),
            published_at=candidate.published_at,
            source_metadata=metadata,
            rss_fingerprint_seen=candidate.fingerprint,
            detail_status="failed",
            raw_data=metadata.copy(),
        )
        return listing

    def _parse_detail(self, html: str, final_url: str) -> _DetailData:
        parser = SscomDetailParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception as exc:
            raise SscomError("Malformed detail HTML") from exc
        if not parser.saw_message:
            raise SscomError("Listing content not found")
        canonical = final_url
        if parser.canonical_url:
            canonical = _validate_url(urljoin(final_url, parser.canonical_url), listing=True)
        description = _clean_text(" ".join(parser.description_parts))[:DESCRIPTION_LIMIT]
        condition_text = (
            f"{parser.fields.get('condition', '')} {parser.fields.get('category', '')}"
        )
        return _DetailData(
            description=description,
            price=_parse_price(parser.price_text),
            location=parser.fields.get("location", "")[:512],
            published_at=_parse_detail_date(parser.date_text),
            condition=_map_condition(condition_text),
            model=parser.fields.get("model", "")[:256],
            year=parser.fields.get("year", "")[:32],
            category=parser.fields.get("category", "")[:256],
            image_urls=parser.image_urls,
            canonical_url=canonical,
        )

    @staticmethod
    def _apply_detail(
        listing: NormalizedListing, detail: _DetailData, fingerprint: str
    ) -> NormalizedListing:
        listing.description = detail.description or listing.description
        listing.price = detail.price
        listing.seller_location = detail.location
        listing.published_at = detail.published_at or listing.published_at
        listing.condition = detail.condition
        listing.image_urls = detail.image_urls or listing.image_urls
        listing.url = detail.canonical_url
        listing.source_metadata.update(
            {"model": detail.model, "year": detail.year, "category": detail.category}
        )
        listing.raw_data = listing.source_metadata.copy()
        listing.rss_fingerprint_enriched = fingerprint
        listing.last_detail_success_at = datetime.now(UTC)
        listing.detail_status = "succeeded"
        return listing

    def _needs_detail(self, candidate: _FeedCandidate) -> bool:
        known = self._known.get(candidate.external_id)
        if known is None or known.last_detail_success_at is None:
            return True
        checked_at = _as_utc(known.last_detail_success_at)
        return (
            known.rss_fingerprint_enriched != candidate.fingerprint
            or datetime.now(UTC) - checked_at >= self._refresh_age
        )

    def _priority(self, candidate: _FeedCandidate) -> tuple[int, float]:
        known = self._known.get(candidate.external_id)
        changed = known is None or known.rss_fingerprint_enriched != candidate.fingerprint
        published = candidate.published_at or datetime.min.replace(tzinfo=UTC)
        return (0 if changed else 1, -published.timestamp())

    async def _fetch(
        self,
        url: str,
        content_types: set[str],
        max_bytes: int,
        *,
        feed: bool = False,
        listing: bool = False,
    ) -> tuple[bytes, str]:
        current = _validate_url(url, feed=feed, listing=listing)
        for redirect_count in range(4):
            response, body = await self._request(current, max_bytes)
            if response.is_redirect:
                if redirect_count == 3:
                    raise SscomError("Too many redirects")
                location = response.headers.get("location")
                if not location:
                    raise SscomError("Redirect missing Location")
                current = _validate_url(urljoin(current, location), feed=feed, listing=listing)
                continue
            media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if media_type not in content_types:
                raise SscomError("Unexpected content type")
            return body, current
        raise SscomError("Too many redirects")

    async def _request(self, url: str, max_bytes: int) -> tuple[httpx.Response, bytes]:
        for attempt in range(3):
            await self._pace()
            try:
                async with self._client.stream("GET", url) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 2:
                            raise SscomError(f"HTTP {response.status_code}")
                        await asyncio.sleep(self._retry_delay(response, attempt))
                        continue
                    if response.status_code >= 400:
                        raise SscomError(f"HTTP {response.status_code}")
                    if response.is_redirect:
                        return response, b""
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > max_bytes:
                        raise SscomError("Response body too large")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise SscomError("Response body too large")
                        chunks.append(chunk)
                    return response, b"".join(chunks)
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise SscomError("Request failed") from exc
                await asyncio.sleep(self._retry_delay(None, attempt))
        raise SscomError("Request failed")

    async def _pace(self) -> None:
        async with self._pace_lock:
            remaining = self._request_delay - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_request_at = time.monotonic()

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            parsed = _parse_retry_after(retry_after)
            if parsed is not None:
                return parsed
        base = self._retry_base_delay * (2**attempt)
        return float(base + random.uniform(0, base / 4 if base else 0))


def _validate_url(url: str, *, feed: bool = False, listing: bool = False) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != FETCH_HOST
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
    ):
        raise SscomError("URL is outside the SS.com allowlist")
    normalized = urlunsplit(("https", FETCH_HOST, parts.path, parts.query, ""))
    if listing and (not parts.path.startswith("/msg/") or not parts.path.endswith(".html")):
        raise SscomError("Invalid listing URL")
    if feed:
        _feed_model(normalized)
    return normalized


def _feed_model(url: str) -> str:
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if len(segments) < 2 or segments[-1] != "rss":
        raise SscomError("Invalid RSS feed URL")
    model = segments[-2]
    if model not in {"signum", "vectra"}:
        raise SscomError("Unsupported RSS model")
    return model


def _listing_model(url: str) -> str:
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    return segments[-2] if len(segments) >= 2 else ""


def _external_id(url: str) -> str:
    filename = Path(urlsplit(url).path).stem
    if not filename or not filename.isascii() or not filename.isalnum():
        raise SscomError("Invalid listing identifier")
    return f"sscom:{filename.lower()}"


def _is_image_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == IMAGE_HOST
        and parts.username is None
        and parts.password is None
        and parts.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    )


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _parse_price(value: str) -> Decimal | None:
    match = re.search(r"(?<!\d)(\d[\d\s\u00a0]*(?:[,.]\d{1,2})?)\s*(?:€|eur)(?!\w)", value, re.I)
    if not match:
        return None
    normalized = match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _map_condition(value: str) -> ListingCondition:
    text = value.casefold()
    parts_terms = (
        "no vienas a/m",
        "rezerves daļas",
        "\u043d\u0430 \u0437\u0430\u043f\u0447\u0430\u0441\u0442\u0438",
        "\u0440\u0430\u0437\u0431\u043e\u0440\u043a\u0430",
    )
    if any(term in text for term in parts_terms):
        return ListingCondition.PARTS_ONLY
    if any(
        term in text
        for term in (
            "jauna",
            "jauns",
            "\u043d\u043e\u0432\u0430\u044f",
            "\u043d\u043e\u0432\u044b\u0439",
        )
    ):
        return ListingCondition.NEW
    if any(term in text for term in ("lietota", "lietots", "\u0431/\u0443")):
        return ListingCondition.USED
    return ListingCondition.UNKNOWN


def _parse_rss_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _parse_detail_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        local = datetime.strptime(value, "%d.%m.%Y %H:%M").replace(tzinfo=ZoneInfo("Europe/Riga"))
        return local.astimezone(UTC)
    except ValueError:
        return None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value).astimezone(UTC)
            return max((retry_at - datetime.now(UTC)).total_seconds(), 0)
        except (TypeError, ValueError):
            return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _error_name(exc: Exception) -> str:
    return str(exc) if isinstance(exc, SscomError) else type(exc).__name__
