from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from irmscher_tracker.api.app import create_app
from irmscher_tracker.db.models import ListingRow


@pytest.fixture
def test_client(settings, session_factory):
    app = create_app(settings, session_factory)
    with TestClient(app) as client:
        yield client


def test_health_endpoint(test_client):
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0",
        "database": "ok",
        "scheduler": "running",
        "ebay_configured": True,
        "telegram_configured": True,
    }


def test_protected_endpoint_requires_valid_token(test_client, settings):
    assert test_client.post("/runs/ebay").status_code == 401
    assert (
        test_client.post("/runs/ebay", headers={"Authorization": "Bearer wrong-token"}).status_code
        == 401
    )
    settings.ebay_enabled = False
    response = test_client.post(
        "/runs/ebay",
        headers={"Authorization": f"Bearer {settings.api_token.get_secret_value()}"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "eBay scanning is disabled"


def test_list_listings_empty(test_client):
    assert test_client.get("/listings").json() == []


@pytest.mark.asyncio
async def test_list_listings_with_data(test_client, db_session):
    listing = _listing("123", "ebay", True)
    db_session.add(listing)
    await db_session.commit()

    response = test_client.get("/listings")
    assert response.status_code == 200
    assert response.json()[0]["title"] == "test listing"


def test_get_listing_not_found(test_client):
    response = test_client.get("/listings/999")
    assert response.status_code == 404


def test_empty_collection_endpoints(test_client):
    assert test_client.get("/matches").json() == []
    assert test_client.get("/search-runs").json() == []


@pytest.mark.asyncio
async def test_listing_filters(test_client, db_session):
    db_session.add_all(
        [
            _listing("1", "ebay", True),
            _listing("2", "ebay", False),
            _listing("3", "kleinanzeigen", True),
        ]
    )
    await db_session.commit()

    by_source = test_client.get("/listings?source=kleinanzeigen").json()
    active = test_client.get("/listings?is_active=true").json()
    assert [row["external_id"] for row in by_source] == ["3"]
    assert {row["external_id"] for row in active} == {"1", "3"}


def _listing(external_id: str, source: str, active: bool) -> ListingRow:
    now = datetime.now(UTC)
    return ListingRow(
        source=source,
        external_id=external_id,
        title="test listing",
        description="",
        url="http://example.test",
        image_urls_json="[]",
        price=Decimal("100"),
        currency="EUR",
        condition="new",
        seller="",
        seller_location="",
        published_at=now,
        first_seen_at=now,
        last_seen_at=now,
        is_active=active,
    )
