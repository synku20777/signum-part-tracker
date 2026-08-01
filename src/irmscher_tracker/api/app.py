from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from irmscher_tracker import __version__
from irmscher_tracker.api.schemas import (
    HealthResponse,
    ListingResponse,
    MatchResponse,
    RunAcceptedResponse,
    SearchRunResponse,
)
from irmscher_tracker.db.engine import get_session_factory
from irmscher_tracker.db.models import ListingRow, PartMatchRow, SearchRunRow
from irmscher_tracker.db.repositories import (
    ListingRepository,
    MatchRepository,
    SearchRunRepository,
)
from irmscher_tracker.domain import ListingCondition, NormalizedListing, Source
from irmscher_tracker.matcher import PartMatcher
from irmscher_tracker.notifications.telegram import TelegramNotifier
from irmscher_tracker.services.alert import AlertService
from irmscher_tracker.services.search import (
    SearchService,
    SourceBusyError,
    SourceRunCoordinator,
)
from irmscher_tracker.settings import Settings, get_settings
from irmscher_tracker.sources.base import SourceAdapter
from irmscher_tracker.sources.ebay import EbayAdapter
from irmscher_tracker.sources.sscom import SscomAdapter, load_feed_urls

logger = logging.getLogger(__name__)
security = HTTPBearer()


@dataclass
class RuntimeState:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    coordinator: SourceRunCoordinator
    scheduler_tasks: dict[Source, asyncio.Task[None]] = field(default_factory=dict)
    manual_tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)


def _search_service(runtime: RuntimeState, notifier: TelegramNotifier | None) -> SearchService:
    settings = runtime.settings
    return SearchService(
        session_factory=runtime.session_factory,
        matcher=PartMatcher(settings.parts_config_path),
        alert_service=AlertService(notifier),
        coordinator=runtime.coordinator,
        score_threshold=settings.minimum_match_score,
        price_change_percent=settings.price_drop_percent,
        max_consecutive_misses=settings.max_consecutive_misses,
    )


def _notifier(settings: Settings) -> TelegramNotifier | None:
    token = settings.telegram_bot_token.get_secret_value()
    if settings.telegram_enabled and token and settings.telegram_chat_id:
        return TelegramNotifier(token, settings.telegram_chat_id)
    return None


def _source_enabled(settings: Settings, source: Source) -> bool:
    return (source is Source.EBAY and settings.ebay_enabled) or (
        source is Source.SSCOM and settings.sscom_enabled
    )


def _source_configured(settings: Settings, source: Source) -> bool:
    if source is Source.EBAY:
        return bool(settings.ebay_client_id and settings.ebay_client_secret.get_secret_value())
    if source is Source.SSCOM:
        try:
            return bool(load_feed_urls(settings.sources_config_path))
        except (OSError, ValueError):
            return False
    return False


async def _known_sscom_listings(runtime: RuntimeState) -> dict[str, NormalizedListing]:
    async with runtime.session_factory() as session:
        rows = await ListingRepository().list_by_source(session, Source.SSCOM.value)
    known: dict[str, NormalizedListing] = {}
    for row in rows:
        try:
            metadata = json.loads(row.source_metadata_json or "{}")
        except json.JSONDecodeError:
            metadata = {"schema_version": 1}
        try:
            condition = ListingCondition(row.condition)
        except ValueError:
            condition = ListingCondition.UNKNOWN
        try:
            images = json.loads(row.image_urls_json or "[]")
        except json.JSONDecodeError:
            images = []
        known[row.external_id] = NormalizedListing(
            source=Source.SSCOM,
            external_id=row.external_id,
            title=row.title,
            description=row.description,
            url=row.url,
            image_urls=images,
            price=row.price,
            currency=row.currency,
            shipping_cost=row.shipping_cost,
            condition=condition,
            seller=row.seller,
            seller_location=row.seller_location,
            published_at=row.published_at,
            source_metadata=metadata,
            rss_fingerprint_seen=row.rss_fingerprint_seen,
            rss_fingerprint_enriched=row.rss_fingerprint_enriched,
            last_detail_success_at=row.last_detail_success_at,
            detail_status=row.detail_status,
            raw_data=metadata.copy(),
        )
    return known


async def _create_ebay_adapter(runtime: RuntimeState) -> SourceAdapter:
    settings = runtime.settings
    return EbayAdapter(
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret.get_secret_value(),
        marketplace_id=settings.ebay_marketplace_id,
        timeout=settings.ebay_api_timeout,
        max_results_per_query=settings.ebay_max_results_per_query,
    )


async def _create_sscom_adapter(runtime: RuntimeState) -> SourceAdapter:
    settings = runtime.settings
    return SscomAdapter(
        feed_urls=load_feed_urls(settings.sources_config_path),
        known_listings=await _known_sscom_listings(runtime),
        timeout=settings.sscom_request_timeout,
        max_detail_requests=settings.sscom_max_detail_requests_per_run,
        detail_refresh_hours=settings.sscom_detail_refresh_hours,
    )


ADAPTER_FACTORIES: dict[Source, Callable[[RuntimeState], Awaitable[SourceAdapter]]] = {
    Source.EBAY: _create_ebay_adapter,
    Source.SSCOM: _create_sscom_adapter,
}


async def _build_adapter(runtime: RuntimeState, source: Source) -> SourceAdapter:
    factory = ADAPTER_FACTORIES.get(source)
    if factory is None:
        raise ValueError(f"Source {source.value} is not implemented")
    return await factory(runtime)


async def _run_prepared(
    adapter: SourceAdapter,
    service: SearchService,
    notifier: TelegramNotifier | None,
    run_id: int,
) -> None:
    try:
        await service.run_reserved(adapter, run_id)
    finally:
        await adapter.close()
        if notifier is not None:
            await notifier.close()


async def _run_source(runtime: RuntimeState, source: Source) -> None:
    notifier = _notifier(runtime.settings)
    adapter: SourceAdapter | None = None
    try:
        adapter = await _build_adapter(runtime, source)
        service = _search_service(runtime, notifier)
        await service.run(adapter)
    finally:
        if adapter is not None:
            await adapter.close()
        if notifier is not None:
            await notifier.close()


async def _scheduler_loop(runtime: RuntimeState, source: Source, interval: int) -> None:
    async def run_once() -> None:
        try:
            await _run_source(runtime, source)
        except SourceBusyError as exc:
            logger.info("Skipping scheduled %s scan; run %d is active", source.value, exc.run_id)
        except Exception:
            logger.exception("Scheduled %s run failed", source.value)

    if runtime.settings.scan_on_startup:
        await run_once()
    while True:
        await asyncio.sleep(interval * 60)
        await run_once()


def create_app(
    settings_override: Settings | None = None,
    session_factory_override: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        settings = settings_override or get_settings()
        session_factory = session_factory_override or get_session_factory(settings.database_url)
        runtime = RuntimeState(
            settings=settings,
            session_factory=session_factory,
            coordinator=SourceRunCoordinator(),
        )
        app.state.runtime = runtime
        async with session_factory() as session:
            await SearchRunRepository().interrupt_stale(session)
            await session.commit()

        intervals = {
            Source.EBAY: settings.search_interval_minutes,
            Source.SSCOM: settings.sscom_interval_minutes,
        }
        for source, interval in intervals.items():
            if _source_enabled(settings, source) and _source_configured(settings, source):
                runtime.scheduler_tasks[source] = asyncio.create_task(
                    _scheduler_loop(runtime, source, interval)
                )
        try:
            yield
        finally:
            tasks = [*runtime.scheduler_tasks.values(), *runtime.manual_tasks.values()]
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="Irmscher Parts Tracker",
        description="Track Irmscher parts for Opel Signum",
        version=__version__,
        lifespan=lifespan,
    )

    def runtime(request: Request) -> RuntimeState:
        return cast(RuntimeState, request.app.state.runtime)

    async def verify_token(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    ) -> None:
        expected = runtime(request).settings.api_token.get_secret_value()
        if not secrets.compare_digest(credentials.credentials, expected):
            raise HTTPException(status_code=401, detail="Invalid API token")

    @app.exception_handler(SourceBusyError)
    async def source_busy_handler(request: Request, exc: SourceBusyError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"An {exc.source.value} scan is already running.",
                "search_run_id": exc.run_id,
            },
        )

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request, response: Response) -> HealthResponse:
        current = runtime(request)
        database: Literal["ok", "error"] = "ok"
        try:
            async with current.session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Database health check failed")
            database = "error"

        scheduler: Literal["running", "stopped"] = (
            "running"
            if all(not task.done() for task in current.scheduler_tasks.values())
            else "stopped"
        )
        status: Literal["ok", "degraded"] = (
            "ok" if database == "ok" and scheduler == "running" else "degraded"
        )
        if status == "degraded":
            response.status_code = 503
        settings = current.settings
        return HealthResponse(
            status=status,
            version=__version__,
            database=database,
            scheduler=scheduler,
            ebay_configured=_source_configured(settings, Source.EBAY),
            sscom_configured=_source_configured(settings, Source.SSCOM),
            telegram_configured=bool(
                settings.telegram_enabled
                and settings.telegram_bot_token.get_secret_value()
                and settings.telegram_chat_id
            ),
        )

    @app.get("/listings", response_model=list[ListingResponse])
    async def list_listings(
        request: Request,
        source: str | None = None,
        is_active: bool | None = None,
        max_price: float | None = None,
        limit: int = Query(default=50, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[ListingResponse]:
        async with runtime(request).session_factory() as session:
            rows = await ListingRepository().list_listings(
                session,
                source=source,
                is_active=is_active,
                limit=limit,
                offset=offset,
                max_price=max_price,
            )
            return [_listing_to_response(row) for row in rows]

    @app.get("/listings/{listing_id}", response_model=ListingResponse)
    async def get_listing(request: Request, listing_id: int) -> ListingResponse:
        async with runtime(request).session_factory() as session:
            row = await ListingRepository().get_by_id(session, listing_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Listing not found")
            return _listing_to_response(row)

    @app.get("/matches", response_model=list[MatchResponse])
    async def list_matches(
        request: Request,
        part_id: str | None = None,
        min_score: int | None = None,
        limit: int = Query(default=50, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[MatchResponse]:
        async with runtime(request).session_factory() as session:
            rows = await MatchRepository().list_matches(
                session,
                part_id=part_id,
                min_score=min_score,
                limit=limit,
                offset=offset,
            )
            return [_match_to_response(row) for row in rows]

    @app.get("/search-runs", response_model=list[SearchRunResponse])
    async def list_search_runs(
        request: Request,
        limit: int = Query(default=20, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[SearchRunResponse]:
        async with runtime(request).session_factory() as session:
            rows = await SearchRunRepository().list_runs(session, limit=limit, offset=offset)
            return [_run_to_response(row) for row in rows]

    @app.get("/search-runs/{run_id}", response_model=SearchRunResponse)
    async def get_search_run(request: Request, run_id: int) -> SearchRunResponse:
        async with runtime(request).session_factory() as session:
            row = await SearchRunRepository().get_by_id(session, run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Search run not found")
            return _run_to_response(row)

    @app.post(
        "/runs/{source}",
        response_model=RunAcceptedResponse,
        status_code=202,
        dependencies=[Depends(verify_token)],
    )
    async def trigger_source_run(request: Request, source: Source) -> RunAcceptedResponse:
        current = runtime(request)
        if source not in {Source.EBAY, Source.SSCOM}:
            raise HTTPException(
                status_code=400, detail=f"Source {source.value} is not implemented"
            )
        if not _source_enabled(current.settings, source):
            raise HTTPException(status_code=400, detail=f"{source.value} scanning is disabled")
        if not _source_configured(current.settings, source):
            raise HTTPException(status_code=400, detail=f"{source.value} is not configured")
        notifier = _notifier(current.settings)
        adapter: SourceAdapter | None = None
        try:
            adapter = await _build_adapter(current, source)
            service = _search_service(current, notifier)
            run_id = await service.reserve(source)
        except Exception:
            if adapter is not None:
                await adapter.close()
            if notifier is not None:
                await notifier.close()
            raise
        task = asyncio.create_task(_run_prepared(adapter, service, notifier, run_id))
        current.manual_tasks[run_id] = task

        def remove_finished(finished: asyncio.Task[None]) -> None:
            current.manual_tasks.pop(run_id, None)
            if not finished.cancelled() and (error := finished.exception()) is not None:
                logger.error("Manual %s run task failed: %s", source.value, type(error).__name__)

        task.add_done_callback(remove_finished)
        return RunAcceptedResponse(search_run_id=run_id, status="running")

    return app


def _listing_to_response(row: ListingRow) -> ListingResponse:
    try:
        image_urls = json.loads(row.image_urls_json) if row.image_urls_json else []
    except json.JSONDecodeError:
        image_urls = []
    return ListingResponse(
        id=row.id,
        source=row.source,
        external_id=row.external_id,
        title=row.title,
        description=row.description or "",
        url=row.url,
        image_urls=image_urls,
        price=row.price,
        currency=row.currency,
        shipping_cost=row.shipping_cost,
        condition=row.condition or "unknown",
        seller=row.seller or "",
        seller_location=row.seller_location or "",
        published_at=row.published_at,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        last_changed_at=row.last_changed_at,
        inactive_at=row.inactive_at,
        consecutive_misses=row.consecutive_misses,
        is_active=row.is_active,
    )


def _match_to_response(row: PartMatchRow) -> MatchResponse:
    return MatchResponse(
        id=row.id,
        listing_id=row.listing_id,
        part_id=row.part_id,
        part_name=row.part_name,
        total_score=row.total_score,
        compatibility_status=row.compatibility_status,
        reasons_json=row.reasons_json,
        algorithm_version=row.algorithm_version,
        matched_at=row.matched_at,
    )


def _run_to_response(row: SearchRunRow) -> SearchRunResponse:
    return SearchRunResponse(
        id=row.id,
        source=row.source,
        started_at=row.started_at,
        finished_at=row.finished_at,
        total_found=row.total_found,
        new_listings=row.new_listings,
        updated_listings=row.updated_listings,
        matches_found=row.matches_found,
        alerts_sent=row.alerts_sent,
        status=row.status,
    )
