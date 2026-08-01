from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from irmscher_tracker.api.app import _state, create_app
from irmscher_tracker.db.models import ListingRow


@pytest.fixture
def app():
    return create_app()

@pytest.fixture
def test_client(app):
    return TestClient(app)

@pytest.fixture(autouse=True)
def setup_state(settings, session_factory):
    _state["settings"] = settings
    _state["session_factory"] = session_factory
    yield
    _state.clear()

def test_health_endpoint(test_client):
    response = test_client.get("/health")
    assert response.status_code == 200
    assert "version" in response.json()
    assert response.json()["status"] == "ok"

def test_list_listings_empty(test_client):
    response = test_client.get("/listings")
    assert response.status_code == 200
    assert response.json() == []

@pytest.mark.asyncio
async def test_list_listings_with_data(test_client, db_session):
    listing = ListingRow(
        source="ebay",
        external_id="123",
        title="test listing",
        description="",
        url="http",
        image_url="",
        price=Decimal("100"),
        currency="EUR",
        shipping_cost=Decimal("10"),
        condition="new",
        seller="",
        seller_location="",
        published_at=datetime.now(UTC),
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        is_active=True
    )
    db_session.add(listing)
    await db_session.commit()

    response = test_client.get("/listings")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["title"] == "test listing"

def test_get_listing_not_found(test_client):
    response = test_client.get("/listings/999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Listing not found"

def test_list_matches_empty(test_client):
    response = test_client.get("/matches")
    assert response.status_code == 200
    assert response.json() == []

def test_search_runs_empty(test_client):
    response = test_client.get("/search-runs")
    assert response.status_code == 200
    assert response.json() == []

@pytest.mark.asyncio
async def test_list_listings_filter_source(test_client, db_session):
    l1 = ListingRow(
        source="ebay",
        external_id="1",
        title="t1",
        description="",
        url="http",
        price=Decimal("100"),
        currency="EUR",
        condition="new",
        seller="",
        seller_location="",
        published_at=datetime.now(UTC),
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        is_active=True
    )
    l2 = ListingRow(
        source="kleinanzeigen",
        external_id="2",
        title="t2",
        description="",
        url="http",
        price=Decimal("100"),
        currency="EUR",
        condition="new",
        seller="",
        seller_location="",
        published_at=datetime.now(UTC),
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        is_active=True
    )
    db_session.add_all([l1, l2])
    await db_session.commit()

    response = test_client.get("/listings?source=ebay")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["source"] == "ebay"

@pytest.mark.asyncio
async def test_list_listings_filter_active(test_client, db_session):
    l1 = ListingRow(
        source="ebay",
        external_id="1",
        title="t1",
        description="",
        url="http",
        price=Decimal("100"),
        currency="EUR",
        condition="new",
        seller="",
        seller_location="",
        published_at=datetime.now(UTC),
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        is_active=True
    )
    l2 = ListingRow(
        source="ebay",
        external_id="2",
        title="t2",
        description="",
        url="http",
        price=Decimal("100"),
        currency="EUR",
        condition="new",
        seller="",
        seller_location="",
        published_at=datetime.now(UTC),
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        is_active=False
    )
    db_session.add_all([l1, l2])
    await db_session.commit()

    response = test_client.get("/listings?is_active=true")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["is_active"] is True
