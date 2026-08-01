from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
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
    SearchHit,
    SearchRunResult,
    SearchRunStatus,
    Source,
)
from irmscher_tracker.matcher import PartMatcher
from irmscher_tracker.services.alert import AlertService
from irmscher_tracker.sources.base import SourceAdapter

logger = logging.getLogger(__name__)


class SourceBusyError(Exception):
    def __init__(self, source: Source, run_id: int) -> None:
        super().__init__(f"A {source.value} scan is already running")
        self.source = source
        self.run_id = run_id


class SourceRunCoordinator:
    """Atomically tracks the active run for each source in this process."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._active_runs: dict[Source, int] = {}

    async def reserve(
        self,
        source: Source,
        create_run: Callable[[], Awaitable[int]],
    ) -> int:
        async with self._guard:
            active_run_id = self._active_runs.get(source)
            if active_run_id is not None:
                raise SourceBusyError(source, active_run_id)
            run_id = await create_run()
            self._active_runs[source] = run_id
            return run_id

    async def release(self, source: Source, run_id: int) -> None:
        async with self._guard:
            if self._active_runs.get(source) == run_id:
                del self._active_runs[source]

    def active_run_id(self, source: Source) -> int | None:
        return self._active_runs.get(source)


class SearchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        matcher: PartMatcher,
        alert_service: AlertService,
        coordinator: SourceRunCoordinator,
        score_threshold: int = 50,
        price_change_percent: Decimal = Decimal("5.0"),
        max_consecutive_misses: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._matcher = matcher
        self._alert_service = alert_service
        self._coordinator = coordinator
        self._score_threshold = score_threshold
        self._price_change_percent = price_change_percent
        self._max_consecutive_misses = max_consecutive_misses
        self._listing_repo = ListingRepository()
        self._snapshot_repo = SnapshotRepository()
        self._match_repo = MatchRepository()
        self._search_run_repo = SearchRunRepository()
        self._notification_repo = NotificationRepository()
        self._deduplicator = Deduplicator(self._listing_repo, self._snapshot_repo)

    async def run(self, adapter: SourceAdapter) -> SearchRunResult:
        source = adapter.source_name
        run_id = await self.reserve(source)
        return await self.run_reserved(adapter, run_id)

    async def reserve(self, source: Source) -> int:
        """Atomically create and reserve a run before work starts."""

        async def create_run() -> int:
            async with self._session_factory() as session:
                row = await self._search_run_repo.create(session, source.value)
                await session.commit()
                return row.id

        return await self._coordinator.reserve(source, create_run)

    async def run_reserved(self, adapter: SourceAdapter, run_id: int) -> SearchRunResult:
        """Execute a run previously created by :meth:`reserve`."""
        start = time.monotonic()
        source = adapter.source_name
        result = SearchRunResult(source=source, run_id=run_id)

        try:
            queries = self._matcher.get_search_queries()
            search_result = await adapter.search(queries)
            result.total_found = len(search_result.hits)
            result.errors.extend(
                f"Target {query}: {error}" for query, error in search_result.query_errors.items()
            )

            processed_listing_ids: set[int] = set()
            processing_complete = True
            for hit in search_result.hits:
                try:
                    listing_id = await self._process_hit(hit, result)
                    processed_listing_ids.add(listing_id)
                except Exception:
                    processing_complete = False
                    logger.exception("Error processing listing %s", hit.listing.external_id)
                    result.errors.append(f"Error processing listing {hit.listing.external_id}")

            complete = (
                search_result.discovery_complete
                and search_result.enrichment_complete
                and processing_complete
            )
            result.status = SearchRunStatus.COMPLETED if complete else SearchRunStatus.PARTIAL
            result.duration_seconds = time.monotonic() - start

            async with self._session_factory() as session:
                if search_result.discovery_complete and processing_complete:
                    await self._listing_repo.increment_misses_for_unseen(
                        session,
                        source.value,
                        processed_listing_ids,
                        self._max_consecutive_misses,
                    )
                await self._search_run_repo.complete(session, run_id, result, result.status)
                await session.commit()
        except asyncio.CancelledError:
            result.status = SearchRunStatus.CANCELLED
            await self._finish_abnormally(run_id, result.status, "Run cancelled")
            raise
        except Exception as exc:
            logger.exception("Search run failed for %s", source.value)
            result.errors.append(type(exc).__name__)
            result.status = SearchRunStatus.FAILED
            result.duration_seconds = time.monotonic() - start
            await self._finish_abnormally(run_id, result.status, str(exc))
        finally:
            await self._coordinator.release(source, run_id)

        return result

    async def _finish_abnormally(self, run_id: int, status: SearchRunStatus, error: str) -> None:
        async with self._session_factory() as session:
            await self._search_run_repo.finish_with_status(session, run_id, status, error)
            await session.commit()

    async def _process_hit(self, hit: SearchHit, result: SearchRunResult) -> int:
        listing = hit.listing
        async with self._session_factory() as session:
            (
                db_listing,
                is_new,
                has_changes,
                previous_price,
                was_active,
            ) = await self._deduplicator.process(session, listing)
            for query in sorted(hit.queries):
                await self._listing_repo.record_query_observation(
                    session, db_listing.id, listing.source.value, query
                )

            if is_new:
                result.new_listings += 1
            elif has_changes:
                result.updated_listings += 1

            match = self._matcher.match(listing)
            if (
                match is None
                or not match.has_part_specific_evidence
                or match.total_score <= 0
                or match.compatibility_status == "incompatible"
            ):
                await self._match_repo.delete_for_listing(session, db_listing.id)
                await session.commit()
                return db_listing.id

            match_row = await self._match_repo.upsert(session, db_listing.id, match)
            result.matches_found += 1
            alert = self._determine_alert(
                listing,
                match,
                is_new,
                has_changes,
                previous_price,
                was_active,
            )
            if alert is None:
                await session.commit()
                return db_listing.id

            event_key = self._event_key(alert, listing, db_listing.reactivated_at)
            notification = await self._notification_repo.reserve(
                session,
                listing_id=db_listing.id,
                match_id=match_row.id,
                alert_type=alert.alert_type.value,
                payload=alert.model_dump_json(),
                event_key=event_key,
            )
            await session.commit()
            if notification is None:
                return db_listing.id

            success = await self._alert_service.send(alert)
            await self._notification_repo.finish(
                session,
                notification.id,
                success,
                None if success else "Notification delivery failed",
            )
            await session.commit()
            if success:
                result.alerts_sent += 1
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
        if match.total_score < self._score_threshold:
            return None

        alert_type: AlertType | None = None
        previous_alert_price: Decimal | None = None
        if is_new:
            alert_type = AlertType.NEW_LISTING
        elif not was_active:
            alert_type = AlertType.REACTIVATED
        elif (
            has_changes
            and listing.price is not None
            and previous_price is not None
            and previous_price > 0
            and listing.price < previous_price
        ):
            decrease = ((previous_price - listing.price) / previous_price) * 100
            if decrease >= self._price_change_percent:
                alert_type = AlertType.PRICE_DECREASE
                previous_alert_price = previous_price

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
            previous_price=previous_alert_price,
        )

    @staticmethod
    def _event_key(
        alert: AlertPayload,
        listing: NormalizedListing,
        reactivated_at: datetime | None,
    ) -> str:
        prefix = f"{listing.source.value}:{listing.external_id}:{alert.part_id}"
        if alert.alert_type is AlertType.NEW_LISTING:
            return f"new:{prefix}"
        if alert.alert_type is AlertType.PRICE_DECREASE:
            assert alert.previous_price is not None
            assert alert.price is not None
            old = alert.previous_price.quantize(Decimal("0.01"))
            new = alert.price.quantize(Decimal("0.01"))
            return f"price-drop:{prefix}:{old}:{new}"
        assert reactivated_at is not None
        return f"reactivated:{prefix}:{reactivated_at.isoformat()}"
