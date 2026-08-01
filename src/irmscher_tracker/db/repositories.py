"""Database repositories for the Irmscher Parts Tracker.

Each repository class groups queries for a single entity and always
takes an ``AsyncSession`` as its first argument so callers control
transaction scope.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from irmscher_tracker.db.models import (
    ListingQueryRow,
    ListingRow,
    ListingSnapshotRow,
    NotificationRow,
    PartMatchRow,
    SearchRunRow,
)
from irmscher_tracker.domain import (
    MatchResult,
    NormalizedListing,
    SearchRunResult,
    SearchRunStatus,
)


class ListingRepository:
    """CRUD operations for ``ListingRow``."""

    async def get_by_source_and_external_id(
        self, session: AsyncSession, source: str, external_id: str
    ) -> ListingRow | None:
        stmt = select(ListingRow).where(
            ListingRow.source == source,
            ListingRow.external_id == external_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(
        self, session: AsyncSession, listing: NormalizedListing
    ) -> tuple[ListingRow, bool, Decimal | None, bool]:
        """Insert or update a listing.

        Returns the row, creation flag, previous price, and previous active state.
        """
        now = datetime.now(UTC)
        source_str = listing.source.value
        condition_str = listing.condition.value

        existing = await self.get_by_source_and_external_id(
            session, source_str, listing.external_id
        )

        if existing is not None:
            previous_price = existing.price
            was_active = existing.is_active
            existing.title = listing.title
            existing.description = listing.description
            existing.url = listing.url
            existing.image_urls_json = json.dumps(listing.image_urls)
            existing.price = listing.price
            existing.currency = listing.currency
            existing.shipping_cost = listing.shipping_cost
            existing.condition = condition_str
            existing.seller = listing.seller
            existing.seller_location = listing.seller_location
            existing.published_at = listing.published_at or existing.published_at
            existing.last_seen_at = now
            existing.consecutive_misses = 0
            existing.is_active = True
            existing.inactive_at = None
            if not was_active:
                existing.reactivated_at = now

            await session.flush()
            return existing, False, previous_price, was_active

        row = ListingRow(
            source=source_str,
            external_id=listing.external_id,
            title=listing.title,
            description=listing.description,
            url=listing.url,
            image_urls_json=json.dumps(listing.image_urls),
            price=listing.price,
            currency=listing.currency,
            shipping_cost=listing.shipping_cost,
            condition=condition_str,
            seller=listing.seller,
            seller_location=listing.seller_location,
            published_at=listing.published_at or now,
            last_seen_at=now,
            last_changed_at=now,
            is_active=True,
            consecutive_misses=0,
        )
        session.add(row)
        await session.flush()
        return row, True, None, True

    async def get_by_id(self, session: AsyncSession, listing_id: int) -> ListingRow | None:
        return await session.get(ListingRow, listing_id)

    async def list_listings(
        self,
        session: AsyncSession,
        source: str | None = None,
        is_active: bool | None = None,
        max_price: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ListingRow]:
        stmt = select(ListingRow)
        if source is not None:
            stmt = stmt.where(ListingRow.source == source)
        if is_active is not None:
            stmt = stmt.where(ListingRow.is_active == is_active)
        if max_price is not None:
            stmt = stmt.where(ListingRow.price <= max_price)
        stmt = stmt.order_by(ListingRow.last_seen_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def record_query_observation(
        self, session: AsyncSession, listing_id: int, source: str, query: str
    ) -> None:
        stmt = select(ListingQueryRow).where(
            ListingQueryRow.listing_id == listing_id,
            ListingQueryRow.source == source,
            ListingQueryRow.query == query,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        now = datetime.now(UTC)
        if row is None:
            session.add(
                ListingQueryRow(
                    listing_id=listing_id, source=source, query=query, last_seen_at=now
                )
            )
        else:
            row.last_seen_at = now
        await session.flush()

    async def mark_inactive(self, session: AsyncSession, listing_id: int) -> None:
        row = await self.get_by_id(session, listing_id)
        if row is not None and row.is_active:
            row.is_active = False
            row.inactive_at = datetime.now(UTC)

    async def increment_misses_for_unseen(
        self,
        session: AsyncSession,
        source: str,
        seen_ids: set[int],
        threshold: int,
    ) -> None:
        missing = update(ListingRow).where(
            ListingRow.source == source,
            ListingRow.is_active.is_(True),
        )
        if seen_ids:
            missing = missing.where(ListingRow.id.not_in(seen_ids))
        await session.execute(missing.values(consecutive_misses=ListingRow.consecutive_misses + 1))
        await session.execute(
            update(ListingRow)
            .where(
                ListingRow.source == source,
                ListingRow.is_active.is_(True),
                ListingRow.consecutive_misses >= threshold,
            )
            .values(is_active=False, inactive_at=datetime.now(UTC))
        )


class SnapshotRepository:
    """CRUD operations for ``ListingSnapshotRow``."""

    async def get_latest(
        self, session: AsyncSession, listing_id: int
    ) -> ListingSnapshotRow | None:
        stmt = (
            select(ListingSnapshotRow)
            .where(ListingSnapshotRow.listing_id == listing_id)
            .order_by(ListingSnapshotRow.captured_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_if_changed(
        self,
        session: AsyncSession,
        listing_id: int,
        listing: NormalizedListing,
    ) -> ListingSnapshotRow | None:
        """Create a snapshot only when tracked fields differ from the latest."""
        latest = await self.get_latest(session, listing_id)
        condition_str = listing.condition.value

        def canonical_decimal(value: Decimal | None) -> str | None:
            return str(value.quantize(Decimal("0.01"))) if value is not None else None

        import re

        canonical_url = listing.url.strip()
        # normalize whitespace
        canonical_title = re.sub(r"\s+", " ", listing.title.strip())
        canonical_desc = re.sub(r"\s+", " ", listing.description.strip())

        snapshot_payload = {
            "schema_version": 1,
            "title": canonical_title,
            "description": canonical_desc,
            "price": canonical_decimal(listing.price),
            "currency": listing.currency.upper(),
            "shipping_cost": canonical_decimal(listing.shipping_cost),
            "condition": condition_str,
            "seller": listing.seller.strip(),
            "seller_location": listing.seller_location.strip(),
            "image_urls": sorted(listing.image_urls) if listing.image_urls else [],
            "url": canonical_url,
        }

        payload_json = json.dumps(snapshot_payload, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

        if latest is not None and latest.payload_hash == payload_hash:
            return None

        snapshot = ListingSnapshotRow(
            listing_id=listing_id,
            schema_version=1,
            payload_hash=payload_hash,
            title=listing.title,
            description=listing.description,
            price=listing.price,
            currency=listing.currency,
            shipping_cost=listing.shipping_cost,
            condition=condition_str,
            seller=listing.seller,
            seller_location=listing.seller_location,
            image_urls_json=json.dumps(listing.image_urls),
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    async def list_for_listing(
        self, session: AsyncSession, listing_id: int
    ) -> list[ListingSnapshotRow]:
        stmt = (
            select(ListingSnapshotRow)
            .where(ListingSnapshotRow.listing_id == listing_id)
            .order_by(ListingSnapshotRow.captured_at.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


class MatchRepository:
    """CRUD operations for ``PartMatchRow``."""

    async def upsert(
        self, session: AsyncSession, listing_id: int, match: MatchResult
    ) -> PartMatchRow:
        row = await self.get_best_for_listing(session, listing_id)
        if row is None:
            row = PartMatchRow(listing_id=listing_id)
            session.add(row)
        row.part_id = match.part_id
        row.part_name = match.part_name
        row.total_score = match.total_score
        row.compatibility_status = match.compatibility_status
        row.reasons_json = json.dumps([reason.model_dump() for reason in match.reasons])
        row.algorithm_version = match.algorithm_version
        row.matched_at = datetime.now(UTC)
        await session.flush()
        return row

    async def get_best_for_listing(
        self, session: AsyncSession, listing_id: int
    ) -> PartMatchRow | None:
        stmt = (
            select(PartMatchRow)
            .where(PartMatchRow.listing_id == listing_id)
            .order_by(PartMatchRow.total_score.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_matches(
        self,
        session: AsyncSession,
        part_id: str | None = None,
        min_score: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PartMatchRow]:
        stmt = select(PartMatchRow)
        if part_id is not None:
            stmt = stmt.where(PartMatchRow.part_id == part_id)
        if min_score is not None:
            stmt = stmt.where(PartMatchRow.total_score >= min_score)
        stmt = stmt.order_by(PartMatchRow.matched_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())


class NotificationRepository:
    """CRUD operations for ``NotificationRow``."""

    async def reserve(
        self,
        session: AsyncSession,
        listing_id: int,
        match_id: int | None,
        alert_type: str,
        payload: str,
        event_key: str,
    ) -> NotificationRow | None:
        stmt = (
            insert(NotificationRow)
            .values(
                event_key=event_key,
                listing_id=listing_id,
                match_id=match_id,
                alert_type=alert_type,
                payload_json=payload,
                sent_at=datetime.now(UTC),
                success=False,
                error_message=None,
            )
            .on_conflict_do_nothing(index_elements=["event_key"])
            .returning(NotificationRow.id)
        )

        result = await session.execute(stmt)
        await session.flush()
        notification_id = result.scalar_one_or_none()
        if notification_id is None:
            return None
        return await session.get(NotificationRow, notification_id)

    async def finish(
        self,
        session: AsyncSession,
        notification_id: int,
        success: bool,
        error_message: str | None,
    ) -> None:
        row = await session.get(NotificationRow, notification_id)
        if row is not None:
            row.success = success
            row.error_message = error_message

    async def was_notified(self, session: AsyncSession, listing_id: int, alert_type: str) -> bool:
        stmt = (
            select(NotificationRow)
            .where(
                NotificationRow.listing_id == listing_id,
                NotificationRow.alert_type == alert_type,
                NotificationRow.success == True,  # noqa: E712
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_for_listing(
        self, session: AsyncSession, listing_id: int
    ) -> list[NotificationRow]:
        stmt = (
            select(NotificationRow)
            .where(NotificationRow.listing_id == listing_id)
            .order_by(NotificationRow.sent_at.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


class SearchRunRepository:
    """CRUD operations for ``SearchRunRow``."""

    async def create(self, session: AsyncSession, source: str) -> SearchRunRow:
        row = SearchRunRow(
            source=source,
            started_at=datetime.now(UTC),
            status="running",
            total_found=0,
            new_listings=0,
            updated_listings=0,
            matches_found=0,
            alerts_sent=0,
        )
        session.add(row)
        await session.flush()
        return row

    async def complete(
        self,
        session: AsyncSession,
        run_id: int,
        result: SearchRunResult,
        status: SearchRunStatus = SearchRunStatus.COMPLETED,
    ) -> None:
        row = await session.get(SearchRunRow, run_id)
        if row is not None:
            row.finished_at = datetime.now(UTC)
            row.total_found = result.total_found
            row.new_listings = result.new_listings
            row.updated_listings = result.updated_listings
            row.matches_found = result.matches_found
            row.alerts_sent = result.alerts_sent
            row.errors_json = json.dumps(result.errors) if result.errors else None
            row.status = status.value

    async def fail(self, session: AsyncSession, run_id: int, error: str) -> None:
        row = await session.get(SearchRunRow, run_id)
        if row is not None:
            row.finished_at = datetime.now(UTC)
            row.errors_json = json.dumps({"error": error})
            row.status = SearchRunStatus.FAILED.value

    async def finish_with_status(
        self,
        session: AsyncSession,
        run_id: int,
        status: SearchRunStatus,
        error: str | None = None,
    ) -> None:
        row = await session.get(SearchRunRow, run_id)
        if row is not None:
            row.finished_at = datetime.now(UTC)
            row.status = status.value
            row.errors_json = json.dumps({"error": error}) if error else None

    async def interrupt_stale(self, session: AsyncSession) -> None:
        await session.execute(
            update(SearchRunRow)
            .where(SearchRunRow.status == SearchRunStatus.RUNNING.value)
            .values(
                finished_at=datetime.now(UTC),
                status=SearchRunStatus.INTERRUPTED.value,
                errors_json=json.dumps({"error": "Interrupted by tracker restart"}),
            )
        )

    async def list_runs(
        self, session: AsyncSession, limit: int = 100, offset: int = 0
    ) -> list[SearchRunRow]:
        stmt = (
            select(SearchRunRow)
            .order_by(SearchRunRow.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
