from __future__ import annotations

import hashlib
import io
import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select

from irmscher_tracker.api.schemas import ManualReviewRequest
from irmscher_tracker.db.models import ListingImageRow, PartMatchRow
from irmscher_tracker.db.repositories import ListingRepository
from irmscher_tracker.domain import ListingCondition, NormalizedListing, Source
from irmscher_tracker.services.review import ReferenceImageStore, ReviewService
from irmscher_tracker.services.review_export import DatasetExportError, ReviewDatasetExporter
from irmscher_tracker.services.review_integrity import ReviewIntegrityService


def _listing(external_id: str) -> NormalizedListing:
    return NormalizedListing(
        source=Source.EBAY,
        external_id=external_id,
        title=f"Irmscher roof spoiler {external_id}",
        description="Candidate",
        url=f"https://www.ebay.de/itm/{external_id}",
        image_urls=[f"https://i.ebayimg.com/{external_id}.jpg"],
        price=Decimal("100"),
        currency="EUR",
        condition=ListingCondition.USED,
        published_at=datetime.now(UTC),
    )


def _jpeg() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 10), "red").save(output, "JPEG")
    return output.getvalue()


def test_reference_submission_requires_privacy_confirmation_and_valid_reason() -> None:
    with pytest.raises(ValidationError):
        ManualReviewRequest(
            outcome="confirmed",
            selected_part_id="roof-spoiler",
            references=[{"listing_image_id": 1, "label": "positive"}],
        )
    with pytest.raises(ValidationError):
        ManualReviewRequest(
            outcome="confirmed",
            selected_part_id="roof-spoiler",
            decision_reason="wrong-part",
        )


@pytest.mark.asyncio
async def test_queue_modes_provenance_and_configured_targets(
    session_factory, matcher, settings, tmp_path: Path
) -> None:
    configured = settings.model_copy(
        update={
            "review_campaign_target": 0,
            "review_confirmed_listings_target": 0,
            "review_positive_references_target": 0,
            "review_negative_listings_target": 0,
            "review_negative_references_target": 1,
        }
    )
    store = ReferenceImageStore(tmp_path)
    service = ReviewService(session_factory, matcher, store, configured)
    async with session_factory() as session:
        high, *_ = await ListingRepository().upsert(session, _listing("high"))
        low, *_ = await ListingRepository().upsert(session, _listing("low"))
        unmatched, *_ = await ListingRepository().upsert(session, _listing("unmatched"))
        session.add_all(
            [
                PartMatchRow(
                    listing_id=high.id,
                    part_id="roof-spoiler",
                    part_name="Roof spoiler",
                    total_score=75,
                    compatibility_status="probable",
                    reasons_json="[]",
                    algorithm_version="1.0",
                    matched_at=datetime.now(UTC),
                ),
                PartMatchRow(
                    listing_id=low.id,
                    part_id="roof-spoiler",
                    part_name="Roof spoiler",
                    total_score=25,
                    compatibility_status="probable",
                    reasons_json="[]",
                    algorithm_version="1.0",
                    matched_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()
        high_id, low_id, unmatched_id = high.id, low.id, unmatched.id

    high_queue = await service.queue(mode="matched-high-confidence")
    low_queue = await service.queue(mode="matched-low-confidence")
    unmatched_queue = await service.queue(mode="unmatched-broad-candidates")
    assert [item.listing_id for item in high_queue.items] == [high_id]
    assert [item.listing_id for item in low_queue.items] == [low_id]
    assert [item.listing_id for item in unmatched_queue.items] == [unmatched_id]

    first = await service.submit(
        high_id,
        ManualReviewRequest(
            outcome="uncertain",
            decision_reason="insufficient-angle",
            review_ui_version="review-ui-v2",
            created_from_queue_mode="matched-high-confidence",
        ),
    )
    second = await service.submit(
        high_id,
        ManualReviewRequest(
            outcome="uncertain",
            decision_reason="low-resolution",
            created_from_queue_mode="uncertain-recheck",
        ),
    )
    assert second.review.previous_review_id == first.review.id
    assert second.review.reviewer_version == "manual-review-v1"
    assert second.review.created_from_queue_mode == "uncertain-recheck"
    assert (await service.queue(mode="uncertain-recheck")).items[0].listing_id == high_id

    await service.submit(
        low_id,
        ManualReviewRequest(
            outcome="confirmed",
            selected_part_id="roof-spoiler",
            decision_reason="visual-shape-match",
        ),
    )
    needs_positive = await service.queue(mode="confirmed-needs-positive-images")
    assert needs_positive.items[0].listing_id == low_id
    assert (await service.queue(mode="part-needs-negatives")).total >= 1
    progress = await service.progress()
    assert progress.campaign_complete is True
    assert progress.targets.campaign_reviews == 0
    await store.close()


@pytest.mark.asyncio
async def test_integrity_repair_and_deterministic_private_export(
    session_factory, matcher, settings, tmp_path: Path
) -> None:
    body = _jpeg()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=body
            )
        )
    )
    store = ReferenceImageStore(tmp_path, client)
    service = ReviewService(session_factory, matcher, store, settings)
    async with session_factory() as session:
        listing, *_ = await ListingRepository().upsert(session, _listing("export"))
        await session.commit()
        listing_id = listing.id
        image_id = await session.scalar(
            select(ListingImageRow.id).where(ListingImageRow.listing_id == listing_id)
        )
    assert image_id is not None
    created = await service.submit(
        listing_id,
        ManualReviewRequest(
            outcome="confirmed",
            selected_part_id="roof-spoiler",
            decision_reason="visual-shape-match",
            contact_information_checked=True,
            references=[
                {
                    "listing_image_id": image_id,
                    "label": "positive",
                    "view": "rear",
                    "context": "fitted",
                    "quality": "good",
                    "obstruction": "none",
                }
            ],
        ),
    )
    reference = created.references[0].reference
    assert reference is not None and reference.privacy_checked_at is not None
    integrity = ReviewIntegrityService(
        session_factory, tmp_path, {part.id for part in matcher.parts}
    )
    assert (await integrity.check()).status == "ok"

    stale = tmp_path / "references" / "roof-spoiler" / ".reference-old.tmp"
    stale.write_bytes(b"temporary")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    assert (await integrity.check()).status == "warning"
    assert (await integrity.check(repair=True)).status == "ok"
    assert not stale.exists()

    destination = tmp_path / "dataset"
    exporter = ReviewDatasetExporter(
        session_factory,
        tmp_path,
        settings.parts_config_path,
        matcher,
        service,
        integrity,
    )
    await exporter.export(destination, allow_integrity_errors=False)
    manifest_text = (destination / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["reference_count"] == 1
    assert "seller" not in manifest_text.casefold()
    assert "Irmscher roof spoiler export" not in manifest_text
    for line in (destination / "checksums.sha256").read_text().splitlines():
        expected, relative = line.split("  ", 1)
        assert hashlib.sha256((destination / relative).read_bytes()).hexdigest() == expected

    reference_file = tmp_path / "references" / "roof-spoiler" / f"{reference.content_sha256}.webp"
    reference_file.write_bytes(b"corrupt")
    assert (await integrity.check()).status == "error"
    with pytest.raises(DatasetExportError):
        await exporter.export(tmp_path / "blocked", allow_integrity_errors=False)
    await exporter.export(tmp_path / "allowed", allow_integrity_errors=True)
    assert json.loads((tmp_path / "allowed" / "manifest.json").read_text())["reference_count"] == 0
    await client.aclose()
