import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import irmscher_tracker.api.app as app_module
from irmscher_tracker.api.app import create_app
from irmscher_tracker.db.models import ListingImageRow, ListingRow
from irmscher_tracker.domain import Source, SourceSearchResult
from irmscher_tracker.sources.base import SourceAdapter


class ApiFakeAdapter(SourceAdapter):
    def __init__(self, block: bool = False) -> None:
        self.block = block

    @property
    def source_name(self) -> Source:
        return Source.SSCOM

    async def search(self, queries: list[str]) -> SourceSearchResult:
        del queries
        if self.block:
            await asyncio.sleep(60)
        return SourceSearchResult()

    async def close(self) -> None:
        return None


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
        "sscom_configured": True,
        "telegram_configured": True,
        "ebay_environment": "sandbox",
        "ebay_deletion_callback_configured": False,
        "ebay_deletion_worker": "disabled",
        "ebay_deletion_pending": 0,
        "ebay_deletion_oldest_pending_seconds": None,
    }


def test_protected_endpoint_requires_valid_token(test_client, settings):
    for headers in (
        {},
        {"Authorization": "Basic malformed"},
        {"Authorization": "Bearer wrong-token"},
    ):
        response = test_client.post("/runs/ebay", headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    settings.ebay_enabled = False
    response = test_client.post(
        "/runs/ebay",
        headers={"Authorization": f"Bearer {settings.api_token.get_secret_value()}"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "ebay scanning is disabled"


def test_review_shell_is_public_but_review_data_is_protected(test_client, settings):
    page = test_client.get("/review")
    assert page.status_code == 200
    assert settings.api_token.get_secret_value() not in page.text
    assert page.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert page.headers["X-Frame-Options"] == "DENY"
    assert test_client.get("/review/assets/review.js").status_code == 200

    for path in ("/review/parts", "/review/progress", "/review/queue", "/review/references"):
        response = test_client.get(path)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    response = test_client.get(
        "/review/parts",
        headers={"Authorization": f"Bearer {settings.api_token.get_secret_value()}"},
    )
    assert response.status_code == 200
    assert any(part["id"] == "front-lip" for part in response.json())


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


@pytest.mark.asyncio
async def test_listing_api_serializes_missing_price(test_client, db_session):
    listing = _listing("no-price", "sscom", True)
    listing.price = None
    db_session.add(listing)
    await db_session.commit()
    response = test_client.get("/listings?source=sscom")
    assert response.status_code == 200
    assert response.json()[0]["price"] is None


@pytest.mark.asyncio
async def test_review_queue_and_append_only_review(test_client, settings, db_session):
    listing = _listing("review-api", "ebay", True)
    db_session.add(listing)
    await db_session.flush()
    now = datetime.now(UTC)
    db_session.add(
        ListingImageRow(
            listing_id=listing.id,
            source_url="https://i.ebayimg.com/review.jpg",
            position=0,
            is_current=True,
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    queue = test_client.get("/review/queue", headers=headers)
    assert queue.status_code == 200
    assert queue.json()["total"] == 1
    assert (
        test_client.get("/review/queue?match_state=matched", headers=headers).json()["total"] == 0
    )
    assert (
        test_client.get("/review/queue?match_state=unmatched", headers=headers).json()["total"]
        == 1
    )

    invalid = test_client.post(
        f"/review/listings/{listing.id}",
        headers=headers,
        json={"outcome": "confirmed", "selected_part_id": "missing-part"},
    )
    assert invalid.status_code == 400
    created = test_client.post(
        f"/review/listings/{listing.id}",
        headers=headers,
        json={"outcome": "uncertain", "notes": "  inspect later  "},
    )
    assert created.status_code == 200
    assert created.json()["review"]["notes"] == "inspect later"
    progress = test_client.get("/review/progress", headers=headers).json()
    assert progress["reviewed_listings"] == 1
    assert progress["outcomes"]["uncertain"] == 1
    assert len(progress["parts"]) == 9
    assert "seller" not in str(progress)
    detail = test_client.get(f"/review/listings/{listing.id}", headers=headers).json()
    assert detail["review_history_count"] == 1
    assert detail["review_history"][0]["outcome"] == "uncertain"


def test_get_listing_not_found(test_client):
    response = test_client.get("/listings/999")
    assert response.status_code == 404


def test_empty_collection_endpoints(test_client):
    assert test_client.get("/matches").json() == []
    assert test_client.get("/search-runs").json() == []


def test_async_source_run_can_be_polled(test_client, settings, monkeypatch):
    settings.sscom_enabled = True

    async def adapter_factory(runtime, source):
        del runtime, source
        return ApiFakeAdapter()

    monkeypatch.setattr(app_module, "_build_adapter", adapter_factory)
    headers = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    response = test_client.post("/runs/sscom", headers=headers)
    assert response.status_code == 202
    run_id = response.json()["search_run_id"]
    for _ in range(50):
        run = test_client.get(f"/search-runs/{run_id}")
        if run.json()["status"] != "running":
            break
        time.sleep(0.01)
    assert run.status_code == 200
    assert run.json()["status"] == "completed"


def test_overlapping_source_run_returns_active_id(test_client, settings, monkeypatch):
    settings.sscom_enabled = True

    async def adapter_factory(runtime, source):
        del runtime, source
        return ApiFakeAdapter(block=True)

    monkeypatch.setattr(app_module, "_build_adapter", adapter_factory)
    headers = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    first = test_client.post("/runs/sscom", headers=headers)
    second = test_client.post("/runs/sscom", headers=headers)
    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json() == {
        "detail": "An sscom scan is already running.",
        "search_run_id": first.json()["search_run_id"],
    }


def test_unimplemented_registered_source_is_rejected(test_client, settings):
    headers = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    response = test_client.post("/runs/allegro", headers=headers)
    assert response.status_code == 400


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
        seller_display="",
        seller_location="",
        published_at=now,
        first_seen_at=now,
        last_seen_at=now,
        is_active=active,
    )
