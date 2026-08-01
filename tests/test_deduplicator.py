from decimal import Decimal

import pytest

from irmscher_tracker.db.repositories import ListingRepository, SnapshotRepository
from irmscher_tracker.deduplicator import Deduplicator


@pytest.mark.asyncio
async def test_new_listing_is_created(db_session, sample_listing):
    dedup = Deduplicator(ListingRepository(), SnapshotRepository())
    db_listing, is_new, has_changes, prev_price, was_active = await dedup.process(
        db_session, sample_listing
    )

    assert is_new is True
    assert has_changes is True
    assert prev_price is None
    assert was_active is True
    assert db_listing.id is not None
    assert db_listing.external_id == "12345"


@pytest.mark.asyncio
async def test_duplicate_listing_not_new(db_session, sample_listing):
    dedup = Deduplicator(ListingRepository(), SnapshotRepository())
    await dedup.process(db_session, sample_listing)

    # Process same again
    _, is_new, has_changes, prev_price, _ = await dedup.process(db_session, sample_listing)

    assert is_new is False
    assert has_changes is False
    assert prev_price == sample_listing.price


@pytest.mark.asyncio
async def test_unchanged_snapshot_suppressed(db_session, sample_listing):
    dedup = Deduplicator(ListingRepository(), SnapshotRepository())
    await dedup.process(db_session, sample_listing)

    _, is_new, has_changes, _, _ = await dedup.process(db_session, sample_listing)

    assert is_new is False
    assert has_changes is False


@pytest.mark.asyncio
async def test_price_change_creates_snapshot(db_session, sample_listing):
    dedup = Deduplicator(ListingRepository(), SnapshotRepository())
    await dedup.process(db_session, sample_listing)

    # Change price
    sample_listing.price = Decimal("250.00")
    _, is_new, has_changes, prev_price, _ = await dedup.process(db_session, sample_listing)

    assert is_new is False
    assert has_changes is True
    assert prev_price == Decimal("299.99")


@pytest.mark.asyncio
async def test_title_change_creates_snapshot(db_session, sample_listing):
    dedup = Deduplicator(ListingRepository(), SnapshotRepository())
    await dedup.process(db_session, sample_listing)

    # Change title
    sample_listing.title = "New Title"
    _, is_new, has_changes, prev_price, _ = await dedup.process(db_session, sample_listing)

    assert is_new is False
    assert has_changes is True
    assert prev_price == sample_listing.price
