from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
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
    RunTriggerResponse,
    SearchRunResponse,
)
from irmscher_tracker.db.engine import get_session_factory
from irmscher_tracker.db.models import ListingRow, PartMatchRow, SearchRunRow
from irmscher_tracker.db.repositories import (
    ListingRepository,
    MatchRepository,
    SearchRunRepository,
)
from irmscher_tracker.matcher import PartMatcher
from irmscher_tracker.notifications.telegram import TelegramNotifier
from irmscher_tracker.services.alert import AlertService
from irmscher_tracker.services.search import (
    SearchService,
    SourceBusyError,
    SourceRunCoordinator,
)
from irmscher_tracker.settings import Settings, get_settings
from irmscher_tracker.sources.ebay import EbayAdapter

logger = logging.getLogger(__name__)
security = HTTPBearer()


@dataclass
class RuntimeState:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    coordinator: SourceRunCoordinator
    scheduler_task: asyncio.Task[None] | None = None


async def _run_ebay(runtime: RuntimeState) -> RunTriggerResponse:
    settings = runtime.settings
    client_secret = settings.ebay_client_secret.get_secret_value()
    if not settings.ebay_enabled:
        raise ValueError("eBay scanning is disabled")
    if not settings.ebay_client_id or not client_secret:
        raise ValueError("eBay credentials not configured")

    bot_token = settings.telegram_bot_token.get_secret_value()
    notifier: TelegramNotifier | None = None
    if settings.telegram_enabled and bot_token and settings.telegram_chat_id:
        notifier = TelegramNotifier(bot_token, settings.telegram_chat_id)

    adapter = EbayAdapter(
        client_id=settings.ebay_client_id,
        client_secret=client_secret,
        marketplace_id=settings.ebay_marketplace_id,
        timeout=settings.ebay_api_timeout,
        max_results_per_query=settings.ebay_max_results_per_query,
    )
    service = SearchService(
        session_factory=runtime.session_factory,
        matcher=PartMatcher(settings.parts_config_path),
        alert_service=AlertService(notifier),
        coordinator=runtime.coordinator,
        score_threshold=settings.minimum_match_score,
        price_change_percent=settings.price_drop_percent,
        max_consecutive_misses=settings.max_consecutive_misses,
    )
    try:
        result = await service.run(adapter)
    finally:
        await adapter.close()
        if notifier is not None:
            await notifier.close()

    assert result.run_id is not None
    return RunTriggerResponse(
        search_run_id=result.run_id,
        status=result.status.value,
        message=f"eBay search {result.status.value} with {result.total_found} listings",
        total_found=result.total_found,
        new_listings=result.new_listings,
        matches_found=result.matches_found,
        alerts_sent=result.alerts_sent,
    )


async def _scheduler_loop(runtime: RuntimeState) -> None:
    async def run_once() -> None:
        try:
            await _run_ebay(runtime)
        except SourceBusyError as exc:
            logger.info("Skipping scheduled eBay scan; run %d is active", exc.run_id)
        except Exception:
            logger.exception("Scheduled eBay run failed")

    if runtime.settings.scan_on_startup:
        await run_once()
    while True:
        await asyncio.sleep(runtime.settings.search_interval_minutes * 60)
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

        runtime.scheduler_task = asyncio.create_task(_scheduler_loop(runtime))
        try:
            yield
        finally:
            runtime.scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await runtime.scheduler_task

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
                "detail": "An eBay scan is already running.",
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

        task = current.scheduler_task
        scheduler: Literal["running", "stopped"] = (
            "running" if task is not None and not task.done() else "stopped"
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
            ebay_configured=bool(
                settings.ebay_enabled
                and settings.ebay_client_id
                and settings.ebay_client_secret.get_secret_value()
            ),
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

    @app.post(
        "/runs/ebay",
        response_model=RunTriggerResponse,
        dependencies=[Depends(verify_token)],
    )
    async def trigger_ebay_run(request: Request) -> RunTriggerResponse:
        try:
            return await _run_ebay(runtime(request))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
