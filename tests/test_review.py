from __future__ import annotations

import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from irmscher_tracker.api.schemas import ManualReviewRequest, ReferenceUpdateRequest
from irmscher_tracker.db.models import (
    ListingImageRow,
    ManualReviewRow,
    PartMatchRow,
    ReferenceImageRow,
)
from irmscher_tracker.db.repositories import ListingRepository
from irmscher_tracker.domain import ListingCondition, NormalizedListing, Source
from irmscher_tracker.services.review import (
    ReferenceImageError,
    ReferenceImageStore,
    ReviewConflictError,
    ReviewService,
)


def _image_bytes(format_name: str = "JPEG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), (220, 40, 30)).save(output, format=format_name, comment=b"removed")
    return output.getvalue()


def _listing(images: list[str]) -> NormalizedListing:
    return NormalizedListing(
        source=Source.EBAY,
        external_id="review-item",
        title="Irmscher Signum roof spoiler",
        description="Review candidate",
        url="https://www.ebay.de/itm/review-item",
        image_urls=images,
        price=Decimal("100"),
        currency="EUR",
        condition=ListingCondition.USED,
        published_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_listing_image_sync_preserves_history_and_order(db_session):
    repository = ListingRepository()
    listing = _listing([" https://i.ebayimg.com/a.jpg ", "https://i.ebayimg.com/a.jpg", "invalid"])
    row, *_ = await repository.upsert(db_session, listing)
    await db_session.commit()
    listing.image_urls = ["https://i.ebayimg.com/b.jpg", "https://i.ebayimg.com/a.jpg"]
    await repository.upsert(db_session, listing)
    await db_session.commit()

    images = list(
        (
            await db_session.execute(
                select(ListingImageRow)
                .where(ListingImageRow.listing_id == row.id)
                .order_by(ListingImageRow.source_url)
            )
        ).scalars()
    )
    assert [(image.source_url, image.position, image.is_current) for image in images] == [
        ("https://i.ebayimg.com/a.jpg", 1, True),
        ("https://i.ebayimg.com/b.jpg", 0, True),
    ]
    listing.image_urls = ["https://i.ebayimg.com/b.jpg"]
    await repository.upsert(db_session, listing)
    await db_session.commit()
    await db_session.refresh(images[0])
    assert images[0].is_current is False


@pytest.mark.asyncio
async def test_review_queue_history_and_reference_lifecycle(
    session_factory, matcher, settings, tmp_path: Path
):
    content = _image_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "i.ebayimg.com"
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = ReferenceImageStore(tmp_path, client)
    service = ReviewService(session_factory, matcher, store, settings)
    async with session_factory() as session:
        listing, *_ = await ListingRepository().upsert(
            session, _listing(["https://i.ebayimg.com/item.jpg"])
        )
        session.add(
            PartMatchRow(
                listing_id=listing.id,
                part_id="roof-spoiler",
                part_name="Roof spoiler",
                total_score=75,
                compatibility_status="probable",
                reasons_json="not-json",
                algorithm_version="1.0",
                matched_at=datetime.now(UTC),
            )
        )
        await session.commit()
        image_id = await session.scalar(
            select(ListingImageRow.id).where(ListingImageRow.listing_id == listing.id)
        )
        listing_id = listing.id
    assert image_id is not None

    queue = await service.queue()
    assert queue.total == 1
    assert queue.items[0].deterministic_match is not None
    assert queue.items[0].deterministic_match.reasons == []
    assert queue.items[0].effective_part_id == "roof-spoiler"

    created = await service.submit(
        listing_id,
        ManualReviewRequest(
            outcome="confirmed",
            selected_part_id="roof-spoiler",
            notes="  useful angle  ",
            contact_information_checked=True,
            references=[{"listing_image_id": image_id, "label": "positive"}],
        ),
    )
    assert created.review.notes == "useful angle"
    assert created.references[0].status == "created"
    reference = created.references[0].reference
    assert reference is not None and reference.mime_type == "image/webp"
    stored = tmp_path / "references" / "roof-spoiler" / f"{reference.content_sha256}.webp"
    assert stored.is_file()
    with Image.open(stored) as sanitized:
        assert sanitized.format == "WEBP"
        assert sanitized.size == (12, 8)
        assert not sanitized.info.get("comment")

    repeated = await service.submit(
        listing_id,
        ManualReviewRequest(
            outcome="confirmed",
            selected_part_id="roof-spoiler",
            contact_information_checked=True,
            references=[{"listing_image_id": image_id, "label": "positive"}],
        ),
    )
    assert repeated.references[0].status == "existing"
    negative = await service.submit(
        listing_id,
        ManualReviewRequest(
            outcome="rejected",
            selected_part_id="roof-spoiler",
            contact_information_checked=True,
            references=[{"listing_image_id": image_id, "label": "negative"}],
        ),
    )
    assert negative.references[0].status == "created"
    assert negative.deactivated_positive_reference_ids == [reference.id]

    updated = await service.update_reference(
        reference.id, ReferenceUpdateRequest(notes=" revised ", is_active=False)
    )
    assert updated.notes == "revised"
    assert updated.is_active is False
    negative = await service.submit(
        listing_id,
        ManualReviewRequest(
            outcome="rejected",
            selected_part_id="roof-spoiler",
            contact_information_checked=True,
            references=[{"listing_image_id": image_id, "label": "negative"}],
        ),
    )
    assert negative.references[0].status == "existing"
    with pytest.raises(ReviewConflictError):
        await service.update_reference(reference.id, ReferenceUpdateRequest(is_active=True))

    reviewed = await service.queue(status="reviewed")
    assert reviewed.total == 1
    assert reviewed.items[0].latest_review is not None
    assert reviewed.items[0].effective_part_id == "roof-spoiler"
    assert (await service.detail(listing_id)).review_history_count == 4
    await client.aclose()


@pytest.mark.asyncio
async def test_progress_uses_latest_reviews_active_references_and_match_state(
    session_factory, matcher, settings, tmp_path: Path
):
    service = ReviewService(session_factory, matcher, ReferenceImageStore(tmp_path), settings)
    now = datetime.now(UTC)
    async with session_factory() as session:
        matched, *_ = await ListingRepository().upsert(
            session,
            _listing(
                [
                    "https://i.ebayimg.com/matched.jpg",
                    "https://i.ebayimg.com/matched-side.jpg",
                ]
            ).model_copy(update={"external_id": "matched"}),
        )
        unmatched, *_ = await ListingRepository().upsert(
            session,
            _listing(["https://i.ebayimg.com/unmatched.jpg"]).model_copy(
                update={"external_id": "unmatched"}
            ),
        )
        session.add(
            PartMatchRow(
                listing_id=matched.id,
                part_id="roof-spoiler",
                part_name="Roof spoiler",
                total_score=75,
                compatibility_status="probable",
                reasons_json="[]",
                algorithm_version="1.0",
                matched_at=now,
            )
        )
        session.add_all(
            [
                ManualReviewRow(
                    listing_id=matched.id,
                    outcome="uncertain",
                    selected_part_id=None,
                    notes=None,
                    reviewed_at=now,
                ),
                ManualReviewRow(
                    listing_id=matched.id,
                    outcome="confirmed",
                    selected_part_id="roof-spoiler",
                    notes=None,
                    reviewed_at=now,
                ),
                ManualReviewRow(
                    listing_id=unmatched.id,
                    outcome="rejected",
                    selected_part_id=None,
                    notes=None,
                    reviewed_at=now,
                ),
            ]
        )
        await session.flush()
        latest = await session.scalar(
            select(ManualReviewRow)
            .where(ManualReviewRow.listing_id == matched.id)
            .order_by(ManualReviewRow.id.desc())
        )
        image_ids = list(
            (
                await session.execute(
                    select(ListingImageRow.id).where(ListingImageRow.listing_id == matched.id)
                )
            ).scalars()
        )
        assert latest is not None and len(image_ids) == 2
        session.add_all(
            [
                ReferenceImageRow(
                    listing_image_id=image_ids[0],
                    manual_review_id=latest.id,
                    part_id="roof-spoiler",
                    label="positive",
                    local_path=f"references/roof-spoiler/{'a' * 64}.webp",
                    content_sha256="a" * 64,
                    mime_type="image/webp",
                    width=12,
                    height=8,
                    notes=None,
                    is_active=True,
                    created_at=now,
                ),
                ReferenceImageRow(
                    listing_image_id=image_ids[1],
                    manual_review_id=latest.id,
                    part_id="roof-spoiler",
                    label="positive",
                    local_path=f"references/roof-spoiler/{'c' * 64}.webp",
                    content_sha256="c" * 64,
                    mime_type="image/webp",
                    width=12,
                    height=8,
                    notes=None,
                    is_active=True,
                    created_at=now,
                ),
                ReferenceImageRow(
                    listing_image_id=image_ids[0],
                    manual_review_id=latest.id,
                    part_id="roof-spoiler",
                    label="negative",
                    local_path=f"references/roof-spoiler/{'b' * 64}.webp",
                    content_sha256="b" * 64,
                    mime_type="image/webp",
                    width=12,
                    height=8,
                    notes=None,
                    is_active=False,
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    progress = await service.progress()
    assert progress.reviewed_listings == 2
    assert progress.outcomes.model_dump() == {
        "confirmed": 1,
        "rejected": 1,
        "uncertain": 0,
    }
    roof = next(part for part in progress.parts if part.part_id == "roof-spoiler")
    assert roof.confirmed_listings == 1
    assert roof.positive_references == 2
    assert roof.positive_listings == 1
    assert roof.negative_references == 0
    assert len(progress.parts) == 9
    assert (await service.queue(status="all", match_state="matched")).total == 1
    assert (await service.queue(status="all", match_state="unmatched")).total == 1

    async with session_factory() as session:
        for index in range(97):
            listing, *_ = await ListingRepository().upsert(
                session,
                _listing([]).model_copy(update={"external_id": f"campaign-{index}"}),
            )
            session.add(
                ManualReviewRow(
                    listing_id=listing.id,
                    outcome="uncertain",
                    selected_part_id=None,
                    notes=None,
                    reviewed_at=now,
                )
            )
        await session.commit()
    almost_complete = await service.progress()
    assert almost_complete.reviewed_listings == 99
    assert almost_complete.remaining_reviews == 1
    assert almost_complete.campaign_complete is False
    async with session_factory() as session:
        listing, *_ = await ListingRepository().upsert(
            session,
            _listing([]).model_copy(update={"external_id": "campaign-final"}),
        )
        session.add(
            ManualReviewRow(
                listing_id=listing.id,
                outcome="uncertain",
                selected_part_id=None,
                notes=None,
                reviewed_at=now,
            )
        )
        await session.commit()
    completed = await service.progress()
    assert completed.reviewed_listings == 100
    assert completed.remaining_reviews == 0
    assert completed.campaign_complete is True
    await service._store.close()


@pytest.mark.asyncio
async def test_reference_downloader_rejects_wrong_host_and_cross_host_redirect(tmp_path: Path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"Location": "https://evil.example/image.jpg"}
            )
        )
    )
    store = ReferenceImageStore(tmp_path, client)
    with pytest.raises(ReferenceImageError):
        await store.download("ebay", "https://example.com/image.jpg")
    with pytest.raises(ReferenceImageError):
        await store.download("ebay", "https://i.ebayimg.com/image.jpg")
    await client.aclose()
