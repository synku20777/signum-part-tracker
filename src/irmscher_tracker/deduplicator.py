"""Listing deduplication and snapshot management.

The ``Deduplicator`` decides whether an incoming listing is new or
already known, and creates a snapshot row only when tracked fields
have actually changed.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from irmscher_tracker.db.models import ListingRow
from irmscher_tracker.db.repositories import ListingRepository, SnapshotRepository
from irmscher_tracker.domain import NormalizedListing


class Deduplicator:
    """Deduplicate listings and create snapshots on change."""

    def __init__(
        self,
        listing_repo: ListingRepository,
        snapshot_repo: SnapshotRepository,
    ) -> None:
        self._listing_repo = listing_repo
        self._snapshot_repo = snapshot_repo

    async def process(
        self,
        session: AsyncSession,
        listing: NormalizedListing,
    ) -> tuple[ListingRow, bool, bool, Decimal | None]:
        """Process a listing for deduplication.

        Returns
        -------
        (db_listing, is_new, has_changes, previous_price)
            *previous_price* is the price stored in the database **before**
            the update was applied.  It is ``None`` for brand-new listings.
        """
        source_str = (
            listing.source.value
            if hasattr(listing.source, "value")
            else str(listing.source)
        )

        # Look up existing row BEFORE upserting so we can capture the
        # previous price before it is overwritten.
        existing = await self._listing_repo.get_by_source_and_external_id(
            session, source_str, listing.external_id
        )
        previous_price: Decimal | None = existing.price if existing is not None else None

        db_listing, is_new = await self._listing_repo.upsert(session, listing)

        if is_new:
            # Always create the initial snapshot.
            await self._snapshot_repo.create_if_changed(
                session, db_listing.id, listing
            )
            return db_listing, True, True, None

        # For existing listings, create a snapshot only when tracked
        # fields differ from the latest snapshot.
        snapshot = await self._snapshot_repo.create_if_changed(
            session, db_listing.id, listing
        )
        has_changes = snapshot is not None

        return db_listing, False, has_changes, previous_price
