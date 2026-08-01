from __future__ import annotations

import logging
import time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from irmscher_tracker.db.repositories import (
    ListingRepository,
    MatchRepository,
    NotificationRepository,
    SearchRunRepository,
    SnapshotRepository,
)
from irmscher_tracker.deduplicator import Deduplicator
from irmscher_tracker.domain import (
    AlertPayload,
    AlertType,
    MatchResult,
    NormalizedListing,
    SearchRunResult,
)
from irmscher_tracker.matcher import PartMatcher
from irmscher_tracker.services.alert import AlertService
from irmscher_tracker.sources.base import SourceAdapter

logger = logging.getLogger(__name__)

class SearchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        matcher: PartMatcher,
        alert_service: AlertService,
        score_threshold: int = 50,
        price_change_percent: Decimal = Decimal("5.0"),
    ):
        self._session_factory = session_factory
        self._matcher = matcher
        self._alert_service = alert_service
        self._score_threshold = score_threshold
        self._price_change_percent = price_change_percent
        self._listing_repo = ListingRepository()
        self._snapshot_repo = SnapshotRepository()
        self._match_repo = MatchRepository()
        self._search_run_repo = SearchRunRepository()
        self._notification_repo = NotificationRepository()
        self._deduplicator = Deduplicator(self._listing_repo, self._snapshot_repo)

    async def run(self, adapter: SourceAdapter) -> SearchRunResult:
        """Run a complete search cycle with the given adapter."""
        start = time.monotonic()
        source = adapter.source_name
        result = SearchRunResult(source=source)

        async with self._session_factory() as session:
            run_row = await self._search_run_repo.create(session, source.value)
            await session.commit()
            run_id = run_row.id

        try:
            # Generate queries from matcher
            queries = self._matcher.get_search_queries()
            logger.info("Searching %s with %d queries", source.value, len(queries))

            # Fetch listings
            listings = await adapter.search(queries)
            result.total_found = len(listings)
            logger.info("Found %d listings from %s", len(listings), source.value)

            processed_listing_ids: set[int] = set()
            # Process each listing
            for listing in listings:
                try:
                    db_id = await self._process_listing(session, listing, result)
                    if db_id is not None:
                        processed_listing_ids.add(db_id)
                except Exception:
                    logger.exception("Error processing listing %s", listing.external_id)
                    result.errors.append(f"Error processing {listing.external_id}")

            # Post-run lifecycle logic
            async with self._session_factory() as session:
                import datetime

                from sqlalchemy import update

                from irmscher_tracker.db.models import ListingRow

                if processed_listing_ids:
                    stmt_miss = (
                        update(ListingRow)
                        .where(
                            ListingRow.source == source.value,
                            ListingRow.is_active.is_(True),
                            ListingRow.id.notin_(processed_listing_ids)
                        )
                        .values(consecutive_misses=ListingRow.consecutive_misses + 1)
                    )
                    await session.execute(stmt_miss)

                    stmt_inactive = (
                        update(ListingRow)
                        .where(
                            ListingRow.source == source.value,
                            ListingRow.is_active.is_(True),
                            ListingRow.consecutive_misses >= 3
                        )
                        .values(
                            is_active=False,
                            inactive_at=datetime.datetime.now(datetime.UTC)
                        )
                    )
                    await session.execute(stmt_inactive)

                await self._search_run_repo.complete(session, run_id, result)
                await session.commit()

            result.duration_seconds = time.monotonic() - start

            logger.info(
                "Search complete: %d found, %d new, %d updated, %d matches, %d alerts",
                result.total_found, result.new_listings, result.updated_listings,
                result.matches_found, result.alerts_sent,
            )

        except Exception as e:
            logger.exception("Search run failed for %s", source.value)
            result.errors.append(str(e))
            async with self._session_factory() as session:
                await self._search_run_repo.fail(session, run_id, str(e))
                await session.commit()

        return result

    async def _process_listing(
        self, parent_session: AsyncSession, listing: NormalizedListing, result: SearchRunResult
    ) -> int | None:
        async with self._session_factory() as session:
            # Deduplicate
            db_listing, is_new, has_changes, previous_price = await self._deduplicator.process(
                session, listing
            )

            if is_new:
                result.new_listings += 1
            elif has_changes:
                result.updated_listings += 1

            # Match against parts
            match = self._matcher.match(listing)
            if match is not None and match.total_score > 0:
                await self._match_repo.create(session, db_listing.id, match)
                result.matches_found += 1

                # Determine if alert should be sent
                alert = self._determine_alert(
                    listing, match, is_new, has_changes, previous_price, db_listing.is_active
                )
                if alert is not None:
                    success = await self._alert_service.send(alert)
                    await self._notification_repo.create(
                        session,
                        listing_id=db_listing.id,
                        match_id=None,
                        alert_type=alert.alert_type.value,
                        payload=alert.model_dump_json(),
                        success=success,
                        error_message=None if success else "Notification delivery failed",
                    )
                    if success:
                        result.alerts_sent += 1

            await session.commit()
            return db_listing.id

    def _determine_alert(
        self,
        listing: NormalizedListing,
        match: MatchResult,
        is_new: bool,
        has_changes: bool,
        previous_price: Decimal | None,
        was_active: bool,
    ) -> AlertPayload | None:
        """Determine if an alert should be sent."""
        if match.total_score < self._score_threshold:
            return None

        alert_type: AlertType | None = None
        prev_price: Decimal | None = None

        if is_new:
            alert_type = AlertType.NEW_LISTING
        elif not was_active:
            alert_type = AlertType.REACTIVATED
        elif has_changes and previous_price is not None and listing.price < previous_price:
            decrease_pct = ((previous_price - listing.price) / previous_price) * 100
            if decrease_pct >= self._price_change_percent:
                alert_type = AlertType.PRICE_DECREASE
                prev_price = previous_price

        if alert_type is None:
            return None

        return AlertPayload(
            alert_type=alert_type,
            listing_title=listing.title,
            listing_url=listing.url,
            source=listing.source,
            part_id=match.part_id,
            part_name=match.part_name,
            score=match.total_score,
            score_explanation=match.reasons,
            price=listing.price,
            currency=listing.currency,
            shipping_cost=listing.shipping_cost,
            seller_location=listing.seller_location,
            previous_price=prev_price,
        )
