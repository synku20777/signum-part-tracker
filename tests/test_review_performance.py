from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import insert

from irmscher_tracker.db.models import (
    ListingImageRow,
    ListingRow,
    ManualReviewRow,
    PartMatchRow,
    ReferenceImageRow,
)
from irmscher_tracker.services.review import ReferenceImageStore, ReviewService


@pytest.mark.asyncio
async def test_personal_database_review_queries_stay_bounded(
    session_factory, matcher, settings, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    part_ids = [part.id for part in matcher.parts]
    listings = []
    images = []
    reviews = []
    references = []
    matches = []
    for index in range(1000):
        listing_id = index + 1
        urls = [f"https://i.ebayimg.com/{index}-{position}.jpg" for position in range(5)]
        listings.append(
            {
                "id": listing_id,
                "source": "ebay",
                "external_id": f"performance-{index}",
                "title": f"Candidate {index}",
                "description": "",
                "url": f"https://www.ebay.de/itm/{index}",
                "image_urls_json": json.dumps(urls),
                "price": None,
                "currency": "EUR",
                "shipping_cost": None,
                "condition": "used",
                "seller_display": "",
                "seller_identifier": None,
                "seller_identifier_type": None,
                "seller_feedback_score": None,
                "seller_feedback_percentage": None,
                "seller_location": "",
                "seller_anonymized_at": None,
                "published_at": now,
                "first_seen_at": now,
                "last_seen_at": now,
                "last_changed_at": now,
                "inactive_at": None,
                "reactivated_at": None,
                "source_metadata_json": '{"schema_version":1}',
                "rss_fingerprint_seen": None,
                "rss_fingerprint_enriched": None,
                "last_detail_success_at": None,
                "detail_status": "not_applicable",
                "consecutive_misses": 0,
                "is_active": True,
            }
        )
        for position, url in enumerate(urls):
            images.append(
                {
                    "id": index * 5 + position + 1,
                    "listing_id": listing_id,
                    "source_url": url,
                    "position": position,
                    "is_current": True,
                    "first_seen_at": now,
                    "last_seen_at": now,
                }
            )
        part_id = part_ids[index % len(part_ids)]
        reviews.append(
            {
                "id": listing_id,
                "listing_id": listing_id,
                "outcome": "confirmed",
                "selected_part_id": part_id,
                "notes": None,
                "reviewed_at": now,
                "previous_review_id": None,
                "reviewer_version": "manual-review-v1",
                "review_ui_version": "performance",
                "decision_reason": "visual-shape-match",
                "created_from_queue_mode": "api",
            }
        )
        digest = f"{index:064x}"
        references.append(
            {
                "id": listing_id,
                "listing_image_id": index * 5 + 1,
                "manual_review_id": listing_id,
                "part_id": part_id,
                "label": "positive",
                "local_path": f"references/{part_id}/{digest}.webp",
                "content_sha256": digest,
                "mime_type": "image/webp",
                "width": 100,
                "height": 100,
                "notes": None,
                "is_active": True,
                "created_at": now,
                "view": "unknown",
                "context": "unknown",
                "quality": "usable",
                "obstruction": "none",
                "privacy_checked_at": now,
            }
        )
        if index < 500:
            matches.append(
                {
                    "listing_id": listing_id,
                    "part_id": part_id,
                    "part_name": part_id,
                    "total_score": 75,
                    "compatibility_status": "probable",
                    "reasons_json": "[]",
                    "algorithm_version": "1.0",
                    "matched_at": now,
                }
            )

    async with session_factory() as session:
        await session.execute(insert(ListingRow), listings)
        await session.execute(insert(ListingImageRow), images)
        await session.execute(insert(ManualReviewRow), reviews)
        await session.execute(insert(ReferenceImageRow), references)
        await session.execute(insert(PartMatchRow), matches)
        await session.commit()

    store = ReferenceImageStore(tmp_path)
    service = ReviewService(session_factory, matcher, store, settings)

    async def measured(awaitable) -> float:
        started = perf_counter()
        await awaitable
        return perf_counter() - started

    timings = {
        "default_queue": await measured(service.queue(status="all")),
        "unmatched_queue": await measured(service.queue(status="all", match_state="unmatched")),
        "progress": await measured(service.progress()),
        "readiness": await measured(service.dataset_readiness()),
        "reference_gallery": await measured(service.references()),
    }
    assert max(timings.values()) < 5.0, timings
    await store.close()
