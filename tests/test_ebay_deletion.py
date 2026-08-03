import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from irmscher_tracker.api.app import create_app
from irmscher_tracker.db.models import (
    EbayDeletionNotificationRow,
    ListingImageRow,
    ManualReviewRow,
    NotificationRow,
    ReferenceImageRow,
)
from irmscher_tracker.db.repositories import EbayDeletionRepository
from irmscher_tracker.services.ebay_deletion import (
    EbayNotificationVerifier,
    EbaySignatureError,
    EbayVerificationUnavailable,
    notification_correlation,
)
from irmscher_tracker.settings import Settings
from irmscher_tracker.sources.ebay_client import (
    EBAY_ENDPOINTS,
    EbayApplicationTokenProvider,
    EbayEnvironment,
)

CALLBACK_TOKEN = "deletion-verification-token-1234567890"
CALLBACK_URL = "http://localhost:8000/ebay/marketplace-account-deletion"


def _settings(**overrides):
    values = {
        "api_token": "test-api-token-test-api-token-123456",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "ebay_enabled": False,
        "ebay_environment": EbayEnvironment.SANDBOX,
        "ebay_client_id": "client",
        "ebay_client_secret": "secret",
        "ebay_deletion_endpoint_url": CALLBACK_URL,
        "ebay_deletion_verification_token": CALLBACK_TOKEN,
        "config_directory": "config",
        "scan_on_startup": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _payload(notification_id: str = "notification-1") -> dict:
    return {
        "metadata": {
            "topic": "MARKETPLACE_ACCOUNT_DELETION",
            "schemaVersion": "1.0",
            "deprecated": False,
        },
        "notification": {
            "notificationId": notification_id,
            "eventDate": "2026-08-02T10:00:00Z",
            "publishDate": "2026-08-02T10:00:01Z",
            "publishAttemptCount": 1,
            "data": {
                "username": "test_seller",
                "userId": "immutable-user",
                "eiasToken": "legacy-token",
            },
        },
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/ebay/marketplace-account-deletion",
        "https://127.0.0.1/ebay/marketplace-account-deletion",
        "https://10.0.0.1/ebay/marketplace-account-deletion",
        "https://[::1]/ebay/marketplace-account-deletion",
    ],
)
def test_production_callback_rejects_non_public_hosts(url):
    with pytest.raises(ValidationError):
        _settings(
            ebay_environment=EbayEnvironment.PRODUCTION,
            ebay_deletion_endpoint_url=url,
        )


def test_deletion_callback_configuration_is_all_or_nothing():
    with pytest.raises(ValidationError):
        _settings(ebay_deletion_verification_token="")
    with pytest.raises(ValidationError):
        _settings(ebay_deletion_verification_token="short")


def test_production_callback_preserves_exact_public_url():
    endpoint = "https://tracker.example.com/ebay/marketplace-account-deletion"
    configured = _settings(
        ebay_environment=EbayEnvironment.PRODUCTION,
        ebay_deletion_endpoint_url=endpoint,
    )
    assert configured.ebay_deletion_endpoint_url == endpoint
    assert configured.ebay_deletion_callback_configured is True
    assert configured.ebay_deletion_callback_ready is True


@pytest.mark.parametrize(
    "url",
    [
        "https://tracker.example.com/wrong",
        "https://user@tracker.example.com/ebay/marketplace-account-deletion",
        "https://tracker.example.com/ebay/marketplace-account-deletion?secret=value",
    ],
)
def test_callback_rejects_ambiguous_urls(url):
    with pytest.raises(ValidationError):
        _settings(ebay_deletion_endpoint_url=url)


def test_challenge_hash_and_public_access(settings, session_factory):
    settings.ebay_environment = EbayEnvironment.SANDBOX
    settings.ebay_deletion_endpoint_url = CALLBACK_URL
    settings.ebay_deletion_verification_token = SecretStr(CALLBACK_TOKEN)
    app = create_app(settings, session_factory)
    with TestClient(app) as client:
        response = client.get(
            "/ebay/marketplace-account-deletion",
            params={"challenge_code": "challenge-ä"},
        )
    import hashlib

    expected = hashlib.sha256(f"challenge-ä{CALLBACK_TOKEN}{CALLBACK_URL}".encode()).hexdigest()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"challengeResponse": expected}


@pytest.mark.asyncio
@respx.mock
async def test_node_golden_signature_is_verified():
    fixture = json.loads(
        (
            Path(__file__).resolve().parent
            / "fixtures"
            / "ebay"
            / "deletion_signature_golden.json"
        ).read_text(encoding="utf-8")
    )
    endpoints = EBAY_ENDPOINTS[EbayEnvironment.SANDBOX]
    respx.post(endpoints.oauth_url).mock(
        return_value=httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
    )
    respx.get(f"{endpoints.notification_public_key_base_url}golden-key-001").mock(
        return_value=httpx.Response(200, json={"key": fixture["publicKey"]})
    )
    provider = EbayApplicationTokenProvider("client", "secret", EbayEnvironment.SANDBOX, 5)
    verifier = EbayNotificationVerifier(provider, endpoints, 5)
    try:
        await verifier.verify(fixture["payload"], fixture["signatureHeader"])
        changed = json.loads(json.dumps(fixture["payload"]))
        changed["notification"]["publishAttemptCount"] = 2
        with pytest.raises(EbaySignatureError):
            await verifier.verify(changed, fixture["signatureHeader"])
    finally:
        await verifier.close()
        await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_public_key_and_token_fetch_are_single_flight():
    fixture = json.loads(
        (
            Path(__file__).resolve().parent
            / "fixtures"
            / "ebay"
            / "deletion_signature_golden.json"
        ).read_text(encoding="utf-8")
    )
    endpoints = EBAY_ENDPOINTS[EbayEnvironment.SANDBOX]
    token_route = respx.post(endpoints.oauth_url).mock(
        return_value=httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
    )
    key_route = respx.get(f"{endpoints.notification_public_key_base_url}golden-key-001").mock(
        return_value=httpx.Response(200, json={"key": fixture["publicKey"]})
    )
    provider = EbayApplicationTokenProvider("client", "secret", EbayEnvironment.SANDBOX, 5)
    verifier = EbayNotificationVerifier(provider, endpoints, 5)
    try:
        await asyncio.gather(
            verifier.verify(fixture["payload"], fixture["signatureHeader"]),
            verifier.verify(fixture["payload"], fixture["signatureHeader"]),
        )
        assert token_route.call_count == 1
        assert key_route.call_count == 1
    finally:
        await verifier.close()
        await provider.close()


def test_post_statuses_and_duplicate_reservation(settings, session_factory, caplog):
    settings.ebay_environment = EbayEnvironment.SANDBOX
    settings.ebay_deletion_endpoint_url = CALLBACK_URL
    settings.ebay_deletion_verification_token = SecretStr(CALLBACK_TOKEN)
    app = create_app(settings, session_factory)
    caplog.set_level("INFO")
    with TestClient(app) as client:
        verifier = app.state.runtime.ebay_notification_verifier
        verifier.verify = AsyncMock()  # type: ignore[method-assign]
        assert (
            client.post("/ebay/marketplace-account-deletion", content=b"not-json").status_code
            == 400
        )
        first = client.post(
            "/ebay/marketplace-account-deletion",
            json=_payload(),
            headers={"X-EBAY-SIGNATURE": "ignored"},
        )
        duplicate = client.post(
            "/ebay/marketplace-account-deletion",
            json=_payload(),
            headers={"X-EBAY-SIGNATURE": "ignored"},
        )

        async def count_rows() -> int:
            async with session_factory() as session:
                return int(
                    await session.scalar(
                        select(func.count()).select_from(EbayDeletionNotificationRow)
                    )
                    or 0
                )

        assert first.status_code == 204
        assert duplicate.status_code == 204
        assert asyncio.run(count_rows()) == 1
    assert "notification-1" not in caplog.text
    assert notification_correlation("notification-1") in caplog.text


def test_post_rejects_signature_failure(settings, session_factory):
    settings.ebay_environment = EbayEnvironment.SANDBOX
    settings.ebay_deletion_endpoint_url = CALLBACK_URL
    settings.ebay_deletion_verification_token = SecretStr(CALLBACK_TOKEN)
    app = create_app(settings, session_factory)
    with TestClient(app) as client:
        verifier = app.state.runtime.ebay_notification_verifier
        verifier.verify = AsyncMock(  # type: ignore[method-assign]
            side_effect=EbaySignatureError()
        )
        response = client.post(
            "/ebay/marketplace-account-deletion",
            json=_payload(),
            headers={"X-EBAY-SIGNATURE": "bad"},
        )
    assert response.status_code == 412


@pytest.mark.parametrize("signature", [None, "not-base64"])
def test_post_rejects_missing_or_malformed_signature(settings, session_factory, signature):
    settings.ebay_environment = EbayEnvironment.SANDBOX
    settings.ebay_deletion_endpoint_url = CALLBACK_URL
    settings.ebay_deletion_verification_token = SecretStr(CALLBACK_TOKEN)
    app = create_app(settings, session_factory)
    headers = {"X-EBAY-SIGNATURE": signature} if signature is not None else {}
    with TestClient(app) as client:
        response = client.post(
            "/ebay/marketplace-account-deletion", json=_payload(), headers=headers
        )
    assert response.status_code == 412


def test_post_returns_503_when_key_lookup_is_unavailable(settings, session_factory):
    settings.ebay_environment = EbayEnvironment.SANDBOX
    settings.ebay_deletion_endpoint_url = CALLBACK_URL
    settings.ebay_deletion_verification_token = SecretStr(CALLBACK_TOKEN)
    app = create_app(settings, session_factory)
    with TestClient(app) as client:
        verifier = app.state.runtime.ebay_notification_verifier
        verifier.verify = AsyncMock(  # type: ignore[method-assign]
            side_effect=EbayVerificationUnavailable()
        )
        response = client.post(
            "/ebay/marketplace-account-deletion",
            json=_payload(),
            headers={"X-EBAY-SIGNATURE": "valid-shape"},
        )
    assert response.status_code == 503


def test_post_returns_500_when_reservation_fails(settings, session_factory, monkeypatch):
    settings.ebay_environment = EbayEnvironment.SANDBOX
    settings.ebay_deletion_endpoint_url = CALLBACK_URL
    settings.ebay_deletion_verification_token = SecretStr(CALLBACK_TOKEN)
    monkeypatch.setattr(
        EbayDeletionRepository,
        "reserve",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    app = create_app(settings, session_factory)
    with TestClient(app) as client:
        verifier = app.state.runtime.ebay_notification_verifier
        verifier.verify = AsyncMock()  # type: ignore[method-assign]
        response = client.post(
            "/ebay/marketplace-account-deletion",
            json=_payload(),
            headers={"X-EBAY-SIGNATURE": "ignored"},
        )
    assert response.status_code == 500


def test_production_health_requires_deletion_callback(settings, session_factory):
    settings.ebay_environment = EbayEnvironment.PRODUCTION
    settings.ebay_enabled = True
    app = create_app(settings, session_factory)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["ebay_deletion_callback_configured"] is False


@pytest.mark.asyncio
async def test_overdue_deletion_is_unhealthy(settings, session_factory):
    settings.ebay_environment = EbayEnvironment.SANDBOX
    settings.ebay_deletion_endpoint_url = CALLBACK_URL
    settings.ebay_deletion_verification_token = SecretStr(CALLBACK_TOKEN)
    async with session_factory() as session:
        session.add(
            EbayDeletionNotificationRow(
                notification_id="overdue",
                username="seller",
                status="pending",
                received_at=datetime.now(UTC) - timedelta(hours=25),
                next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
                attempt_count=1,
            )
        )
        await session.commit()
    app = create_app(settings, session_factory)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["ebay_deletion_pending"] == 1
    assert response.json()["ebay_deletion_oldest_pending_seconds"] >= 24 * 3600


@pytest.mark.asyncio
async def test_anonymization_scrubs_history_and_prevents_rehydration(db_session, sample_listing):
    sample_listing.seller_display = "test_seller (42, 99.5%)"
    sample_listing.seller_identifier = "test_seller"
    sample_listing.seller_identifier_type = "username_or_user_id"
    sample_listing.seller_feedback_score = 42
    sample_listing.seller_feedback_percentage = Decimal("99.5")
    sample_listing.seller_location = "Seller city"
    sample_listing.image_urls = ["https://i.ebayimg.com/reference.jpg"]
    repository = EbayDeletionRepository()
    from irmscher_tracker.db.repositories import ListingRepository, SnapshotRepository

    listing, _, _, _ = await ListingRepository().upsert(db_session, sample_listing)
    snapshot = await SnapshotRepository().create_if_changed(db_session, listing.id, sample_listing)
    alert = NotificationRow(
        event_key="event",
        listing_id=listing.id,
        alert_type="new_listing",
        payload_json=json.dumps({"seller_location": "Seller city", "username": "test_seller"}),
        sent_at=datetime.now(UTC),
        success=True,
    )
    db_session.add(alert)
    listing_image = await db_session.scalar(
        select(ListingImageRow).where(ListingImageRow.listing_id == listing.id)
    )
    assert listing_image is not None
    review = ManualReviewRow(
        listing_id=listing.id,
        outcome="confirmed",
        selected_part_id="front-lip",
        notes="seller mentioned here",
        reviewed_at=datetime.now(UTC),
        reviewer_version="manual-review-v1",
        review_ui_version="review-ui-v2",
        decision_reason="visual-shape-match",
        created_from_queue_mode="matched-high-confidence",
    )
    db_session.add(review)
    await db_session.flush()
    reference = ReferenceImageRow(
        listing_image_id=listing_image.id,
        manual_review_id=review.id,
        part_id="front-lip",
        label="positive",
        local_path="references/front-lip/" + "a" * 64 + ".webp",
        content_sha256="a" * 64,
        mime_type="image/webp",
        width=10,
        height=10,
        notes="seller identity note",
        is_active=True,
        created_at=datetime.now(UTC),
        view="front",
        context="fitted",
        quality="good",
        obstruction="none",
        privacy_checked_at=datetime.now(UTC),
    )
    db_session.add(reference)
    unrelated = sample_listing.model_copy(
        update={
            "external_id": "unrelated-eias-value",
            "seller_display": "unrelated",
            "seller_identifier": "legacy-eias-token",
            "seller_identifier_type": "username_or_user_id",
        }
    )
    unrelated_row, _, _, _ = await ListingRepository().upsert(db_session, unrelated)
    await repository.reserve(
        db_session,
        notification_id="delete-1",
        username="test_seller",
        user_id=None,
        eias_token="legacy-eias-token",
    )
    await db_session.commit()
    deletion = await db_session.scalar(select(EbayDeletionNotificationRow))
    assert deletion is not None and snapshot is not None

    await repository.anonymize(db_session, deletion)
    await repository.mark_processed(db_session, deletion)
    await db_session.commit()
    await db_session.refresh(listing)
    await db_session.refresh(snapshot)
    await db_session.refresh(alert)
    await db_session.refresh(review)
    await db_session.refresh(reference)

    assert listing.seller_display == ""
    assert listing.seller_identifier is None
    assert listing.seller_location == ""
    assert listing.seller_anonymized_at is not None
    assert snapshot.seller_display == ""
    assert snapshot.seller_location == ""
    assert snapshot.schema_version == 2
    assert len(snapshot.payload_hash) == 64
    assert json.loads(alert.payload_json) == {"seller_location": "", "username": ""}
    assert review.notes is None
    assert review.decision_reason == "visual-shape-match"
    assert reference.notes is None
    assert reference.is_active is True
    assert reference.view == "front"
    assert deletion.status == "processed"
    assert deletion.username is None
    await db_session.refresh(unrelated_row)
    assert unrelated_row.seller_identifier == "legacy-eias-token"
    assert unrelated_row.seller_anonymized_at is None

    sample_listing.seller_display = "test_seller"
    sample_listing.seller_identifier = "test_seller"
    sample_listing.seller_location = "Seller city"
    await ListingRepository().upsert(db_session, sample_listing)
    await db_session.commit()
    await db_session.refresh(listing)
    assert listing.seller_display == ""
    assert listing.seller_identifier is None
    assert listing.seller_location == ""


@pytest.mark.asyncio
async def test_expired_processing_lease_is_recovered(db_session):
    repository = EbayDeletionRepository()
    await repository.reserve(
        db_session,
        notification_id="lease-1",
        username="seller",
        user_id=None,
        eias_token=None,
    )
    await db_session.commit()
    row = await repository.claim_next(db_session)
    assert row is not None
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    await repository.recover_expired(db_session)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_retry_backoff_is_bounded(db_session):
    repository = EbayDeletionRepository()
    row = EbayDeletionNotificationRow(
        notification_id="retry-1",
        username="seller",
        status="processing",
        received_at=datetime.now(UTC),
        attempt_count=100,
    )
    db_session.add(row)
    await db_session.flush()
    before = datetime.now(UTC)
    await repository.retry(db_session, row, "DatabaseError")
    assert row.status == "pending"
    assert row.last_error_code == "DatabaseError"
    assert row.next_attempt_at is not None
    next_attempt = row.next_attempt_at
    if next_attempt.tzinfo is None:
        next_attempt = next_attempt.replace(tzinfo=UTC)
    assert next_attempt <= before + timedelta(seconds=3601)
