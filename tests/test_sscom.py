from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from irmscher_tracker.domain import ListingCondition
from irmscher_tracker.sources.sscom import SscomAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "sscom"
SIGNUM_FEED = "https://www.ss.com/lv/transport/spare-parts/opel/signum/rss/"
SIGNUM_CARS = "https://www.ss.com/lv/transport/cars/opel/signum/rss/"
SPOILER_URL = "https://www.ss.com/msg/lv/transport/spare-parts/opel/signum/abc123.html"
DONOR_URL = "https://www.ss.com/msg/lv/transport/spare-parts/opel/signum/donor1.html"


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _response(name: str, content_type: str) -> httpx.Response:
    return httpx.Response(200, content=_bytes(name), headers={"Content-Type": content_type})


@pytest.mark.asyncio
@respx.mock
async def test_discovers_enriches_deduplicates_and_filters_model_noise():
    respx.get(SIGNUM_FEED).mock(return_value=_response("signum_feed.xml", "text/xml"))
    respx.get(SIGNUM_CARS).mock(return_value=_response("signum_feed.xml", "text/xml"))
    respx.get(SPOILER_URL).mock(return_value=_response("irmscher_spoiler.html", "text/html"))
    respx.get(DONOR_URL).mock(return_value=_response("donor_missing_price.html", "text/html"))
    adapter = SscomAdapter([SIGNUM_FEED, SIGNUM_CARS], request_delay=0, retry_base_delay=0)
    try:
        result = await adapter.search([])
    finally:
        await adapter.close()

    assert result.discovery_complete is True
    assert result.enrichment_complete is True
    assert {hit.listing.external_id for hit in result.hits} == {
        "sscom:abc123",
        "sscom:donor1",
    }
    spoiler = next(hit for hit in result.hits if hit.listing.external_id == "sscom:abc123")
    assert spoiler.queries == {SIGNUM_FEED, SIGNUM_CARS}
    assert spoiler.listing.price == Decimal("140")
    assert spoiler.listing.condition is ListingCondition.USED
    assert spoiler.listing.seller_location == "Rīga"
    assert len(spoiler.listing.image_urls) == 2
    assert spoiler.listing.source_metadata["category"] == "Spoileri"
    assert "private@example.test" not in spoiler.listing.model_dump_json()
    donor = next(hit for hit in result.hits if hit.listing.external_id == "sscom:donor1")
    assert donor.listing.price is None
    assert donor.listing.condition is ListingCondition.PARTS_ONLY


@pytest.mark.asyncio
@respx.mock
async def test_detail_failure_is_enrichment_partial_but_discovery_complete():
    respx.get(SIGNUM_FEED).mock(return_value=_response("signum_feed.xml", "text/xml"))
    respx.get(SPOILER_URL).mock(return_value=httpx.Response(500))
    respx.get(DONOR_URL).mock(return_value=_response("donor_missing_price.html", "text/html"))
    adapter = SscomAdapter([SIGNUM_FEED], request_delay=0, retry_base_delay=0)
    try:
        result = await adapter.search([])
    finally:
        await adapter.close()

    assert result.discovery_complete is True
    assert result.enrichment_complete is False
    assert len(result.hits) == 2
    failed = next(hit.listing for hit in result.hits if hit.listing.external_id == "sscom:abc123")
    assert failed.detail_status == "failed"
    assert failed.price == Decimal("140")


@pytest.mark.asyncio
@respx.mock
async def test_failed_feed_is_discovery_partial():
    respx.get(SIGNUM_FEED).mock(return_value=httpx.Response(404))
    adapter = SscomAdapter([SIGNUM_FEED], request_delay=0, retry_base_delay=0)
    try:
        result = await adapter.search([])
    finally:
        await adapter.close()
    assert result.discovery_complete is False
    assert result.hits == []


@pytest.mark.asyncio
@respx.mock
async def test_detail_budget_defers_enrichment_without_dropping_hits():
    respx.get(SIGNUM_FEED).mock(return_value=_response("signum_feed.xml", "text/xml"))
    adapter = SscomAdapter([SIGNUM_FEED], max_detail_requests=0, request_delay=0)
    try:
        result = await adapter.search([])
    finally:
        await adapter.close()
    assert result.discovery_complete is True
    assert result.enrichment_complete is False
    assert len(result.hits) == 2
    assert all(hit.listing.detail_status == "deferred" for hit in result.hits)


@pytest.mark.asyncio
@respx.mock
async def test_fresh_cached_detail_is_not_refetched():
    respx.get(SIGNUM_FEED).mock(return_value=_response("signum_feed.xml", "text/xml"))
    first_adapter = SscomAdapter([SIGNUM_FEED], max_detail_requests=0, request_delay=0)
    try:
        first = await first_adapter.search([])
    finally:
        await first_adapter.close()
    cached = first.hits[0].listing
    cached.rss_fingerprint_enriched = cached.rss_fingerprint_seen
    cached.last_detail_success_at = datetime.now(UTC)
    cached.detail_status = "succeeded"

    adapter = SscomAdapter(
        [SIGNUM_FEED],
        known_listings={cached.external_id: cached},
        max_detail_requests=0,
        request_delay=0,
    )
    try:
        result = await adapter.search([])
    finally:
        await adapter.close()
    cached_result = next(
        hit.listing for hit in result.hits if hit.listing.external_id == cached.external_id
    )
    assert cached_result.detail_status == "succeeded"


@pytest.mark.asyncio
@respx.mock
async def test_rejects_cross_domain_redirect_and_wrong_content_type():
    respx.get(SIGNUM_FEED).mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/feed.xml"})
    )
    adapter = SscomAdapter([SIGNUM_FEED], request_delay=0)
    try:
        redirected = await adapter.search([])
    finally:
        await adapter.close()
    assert redirected.discovery_complete is False

    with respx.mock:
        respx.get(SIGNUM_FEED).mock(
            return_value=httpx.Response(
                200, text="not xml", headers={"Content-Type": "text/plain"}
            )
        )
        adapter = SscomAdapter([SIGNUM_FEED], request_delay=0)
        try:
            wrong_type = await adapter.search([])
        finally:
            await adapter.close()
    assert wrong_type.discovery_complete is False


@pytest.mark.asyncio
@respx.mock
async def test_retry_after_is_honored_before_success():
    route = respx.get(SIGNUM_FEED).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            _response("empty_feed.xml", "text/xml"),
        ]
    )
    adapter = SscomAdapter([SIGNUM_FEED], request_delay=0, retry_base_delay=0)
    try:
        result = await adapter.search([])
    finally:
        await adapter.close()
    assert result.discovery_complete is True
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_oversized_or_malformed_feed_is_discovery_partial():
    respx.get(SIGNUM_FEED).mock(
        return_value=httpx.Response(
            200,
            content=b"<rss/>",
            headers={"Content-Type": "text/xml", "Content-Length": str(2 * 1024 * 1024 + 1)},
        )
    )
    adapter = SscomAdapter([SIGNUM_FEED], request_delay=0)
    try:
        oversized = await adapter.search([])
    finally:
        await adapter.close()
    assert oversized.discovery_complete is False

    with respx.mock:
        respx.get(SIGNUM_FEED).mock(
            return_value=httpx.Response(
                200, content=b"<rss>", headers={"Content-Type": "text/xml"}
            )
        )
        adapter = SscomAdapter([SIGNUM_FEED], request_delay=0)
        try:
            malformed = await adapter.search([])
        finally:
            await adapter.close()
    assert malformed.discovery_complete is False
