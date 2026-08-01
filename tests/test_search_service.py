import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from irmscher_tracker.db.models import (
    ListingQueryRow,
    ListingRow,
    ListingSnapshotRow,
    NotificationRow,
    PartMatchRow,
)
from irmscher_tracker.domain import SearchHit, Source, SourceSearchResult
from irmscher_tracker.services.alert import AlertService
from irmscher_tracker.services.search import (
    SearchService,
    SourceBusyError,
    SourceRunCoordinator,
)
from irmscher_tracker.sources.base import SourceAdapter


class FakeAdapter(SourceAdapter):
    def __init__(self, result: SourceSearchResult) -> None:
        self.result = result
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    @property
    def source_name(self) -> Source:
        return Source.EBAY

    async def search(self, queries: list[str]) -> SourceSearchResult:
        del queries
        if self.started is not None and self.release is not None:
            self.started.set()
            await self.release.wait()
        return self.result

    async def close(self) -> None:
        return None


def _result(sample_listing, *, complete: bool = True) -> SourceSearchResult:
    return SourceSearchResult(
        hits=[SearchHit(listing=sample_listing, queries={"i3401009"})],
        successful_queries=["i3401009"] if complete else [],
        query_errors={} if complete else {"i3401009": "HTTP 429"},
        complete=complete,
    )


def _service(session_factory, matcher, notifier, coordinator, misses=3):
    return SearchService(
        session_factory=session_factory,
        matcher=matcher,
        alert_service=AlertService(notifier),
        coordinator=coordinator,
        score_threshold=50,
        max_consecutive_misses=misses,
    )


@pytest.mark.asyncio
async def test_repeated_complete_scan_is_idempotent(session_factory, matcher, sample_listing):
    notifier = AsyncMock()
    coordinator = SourceRunCoordinator()
    service = _service(session_factory, matcher, notifier, coordinator)
    adapter = FakeAdapter(_result(sample_listing))

    first = await service.run(adapter)
    second = await service.run(adapter)

    assert first.status.value == "completed"
    assert second.status.value == "completed"
    assert notifier.send_alert.await_count == 1
    async with session_factory() as session:
        counts = []
        for model in (
            ListingRow,
            ListingSnapshotRow,
            PartMatchRow,
            NotificationRow,
            ListingQueryRow,
        ):
            counts.append(await session.scalar(select(func.count()).select_from(model)))
    assert counts == [1, 1, 1, 1, 1]


@pytest.mark.asyncio
async def test_partial_scan_does_not_increment_misses(session_factory, matcher, sample_listing):
    service = _service(session_factory, matcher, AsyncMock(), SourceRunCoordinator(), misses=1)
    await service.run(FakeAdapter(_result(sample_listing)))
    partial = SourceSearchResult(query_errors={"i3401009": "HTTP 429"}, complete=False)
    result = await service.run(FakeAdapter(partial))

    async with session_factory() as session:
        listing = await session.scalar(select(ListingRow))
    assert result.status.value == "partial"
    assert listing is not None
    assert listing.consecutive_misses == 0
    assert listing.is_active is True


@pytest.mark.asyncio
async def test_miss_threshold_and_reactivation_are_applied_once(
    session_factory, matcher, sample_listing
):
    notifier = AsyncMock()
    service = _service(session_factory, matcher, notifier, SourceRunCoordinator(), misses=2)
    await service.run(FakeAdapter(_result(sample_listing)))
    empty = FakeAdapter(SourceSearchResult(complete=True))
    await service.run(empty)
    await service.run(empty)

    async with session_factory() as session:
        listing = await session.scalar(select(ListingRow))
        assert listing is not None
        assert listing.is_active is False

    await service.run(FakeAdapter(_result(sample_listing)))
    await service.run(FakeAdapter(_result(sample_listing)))

    async with session_factory() as session:
        listing = await session.scalar(select(ListingRow))
        notifications = await session.scalar(select(func.count()).select_from(NotificationRow))
    assert listing is not None
    assert listing.is_active is True
    assert listing.consecutive_misses == 0
    assert listing.reactivated_at is not None
    assert notifications == 2
    assert notifier.send_alert.await_count == 2


@pytest.mark.asyncio
async def test_coordinator_rejects_overlapping_source_run(
    session_factory, matcher, sample_listing
):
    coordinator = SourceRunCoordinator()
    first_service = _service(session_factory, matcher, AsyncMock(), coordinator)
    second_service = _service(session_factory, matcher, AsyncMock(), coordinator)
    adapter = FakeAdapter(_result(sample_listing))
    adapter.started = asyncio.Event()
    adapter.release = asyncio.Event()

    running = asyncio.create_task(first_service.run(adapter))
    await adapter.started.wait()
    with pytest.raises(SourceBusyError) as error:
        await second_service.run(FakeAdapter(_result(sample_listing)))
    assert error.value.run_id > 0

    adapter.release.set()
    await running
