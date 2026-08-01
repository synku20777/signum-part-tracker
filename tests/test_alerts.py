from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from irmscher_tracker.domain import (
    AlertPayload,
    AlertType,
    MatchResult,
    NormalizedListing,
    ScoringReason,
    Source,
)
from irmscher_tracker.services.alert import AlertService
from irmscher_tracker.services.search import SearchService, SourceRunCoordinator


@pytest.fixture
def mock_notifier():
    notifier = AsyncMock()
    notifier.send_alert = AsyncMock()
    return notifier


@pytest.fixture
def base_payload():
    return AlertPayload(
        alert_type=AlertType.NEW_LISTING,
        listing_title="Test Part",
        listing_url="http://test.com",
        source=Source.EBAY,
        part_id="test_part",
        part_name="Test Part",
        score=100,
        score_explanation=[ScoringReason(rule="test", points=100)],
        price=Decimal("100.00"),
    )


@pytest.fixture
def search_service():
    return SearchService(
        session_factory=None,
        matcher=None,
        alert_service=None,
        coordinator=SourceRunCoordinator(),
        score_threshold=50,
        price_change_percent=Decimal("5.0"),
    )


@pytest.fixture
def sample_match():
    return MatchResult(
        part_id="part1",
        part_name="Part 1",
        total_score=60,
        compatibility_status="probable",
        reasons=[],
    )


@pytest.fixture
def sample_listing():
    return NormalizedListing(
        source=Source.EBAY, external_id="123", title="Part", url="http://", price=Decimal("100.00")
    )


@pytest.mark.asyncio
async def test_new_listing_triggers_alert(search_service, sample_listing, sample_match):
    alert = search_service._determine_alert(
        listing=sample_listing,
        match=sample_match,
        is_new=True,
        has_changes=False,
        previous_price=None,
        was_active=True,
    )
    assert alert is not None
    assert alert.alert_type == AlertType.NEW_LISTING


@pytest.mark.asyncio
async def test_below_threshold_no_alert(search_service, sample_listing, sample_match):
    sample_match.total_score = 40  # Below threshold of 50
    alert = search_service._determine_alert(
        listing=sample_listing,
        match=sample_match,
        is_new=True,
        has_changes=False,
        previous_price=None,
        was_active=True,
    )
    assert alert is None


@pytest.mark.asyncio
async def test_price_decrease_triggers_alert(search_service, sample_listing, sample_match):
    # Old price 100, new price 90 -> 10% decrease > 5% threshold
    sample_listing.price = Decimal("90.00")
    alert = search_service._determine_alert(
        listing=sample_listing,
        match=sample_match,
        is_new=False,
        has_changes=True,
        previous_price=Decimal("100.00"),
        was_active=True,
    )
    assert alert is not None
    assert alert.alert_type == AlertType.PRICE_DECREASE


@pytest.mark.asyncio
async def test_small_price_change_no_alert(search_service, sample_listing, sample_match):
    # Old price 100, new price 98 -> 2% decrease < 5% threshold
    sample_listing.price = Decimal("98.00")
    alert = search_service._determine_alert(
        listing=sample_listing,
        match=sample_match,
        is_new=False,
        has_changes=True,
        previous_price=Decimal("100.00"),
        was_active=True,
    )
    assert alert is None


@pytest.mark.asyncio
async def test_reactivated_listing_alert(search_service, sample_listing, sample_match):
    alert = search_service._determine_alert(
        listing=sample_listing,
        match=sample_match,
        is_new=False,
        has_changes=False,
        previous_price=None,
        was_active=False,
    )
    assert alert is not None
    assert alert.alert_type == AlertType.REACTIVATED


@pytest.mark.asyncio
async def test_no_notifier_configured(base_payload):
    service = AlertService(notifier=None)
    result = await service.send(base_payload)
    assert result is False


@pytest.mark.asyncio
async def test_notification_failure_recorded(mock_notifier, base_payload):
    mock_notifier.send_alert.side_effect = Exception("API error")
    service = AlertService(notifier=mock_notifier)
    result = await service.send(base_payload)
    assert result is False
