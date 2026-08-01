from __future__ import annotations

import json
import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from irmscher_tracker import __version__
from irmscher_tracker.api.schemas import (
    HealthResponse,
    ListingResponse,
    MatchResponse,
    RunTriggerResponse,
    SearchRunResponse,
)
from irmscher_tracker.db.engine import get_session_factory
from irmscher_tracker.db.repositories import (
    ListingRepository,
    MatchRepository,
    SearchRunRepository,
)
from irmscher_tracker.matcher import PartMatcher
from irmscher_tracker.notifications.telegram import TelegramNotifier
from irmscher_tracker.services.alert import AlertService
from irmscher_tracker.services.search import SearchService
from irmscher_tracker.settings import Settings, get_settings
from irmscher_tracker.sources.ebay import EbayAdapter

logger = logging.getLogger(__name__)

# Store shared state
_state: dict = {}

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    _state["settings"] = settings
    _state["session_factory"] = get_session_factory(settings.database_url)

    # Mark stale runs as failed (runs without finished_at)
    async with _state["session_factory"]() as session:
        from datetime import datetime

        from sqlalchemy import update

        from irmscher_tracker.db.models import SearchRunRow
        stmt = (
            update(SearchRunRow)
            .where(SearchRunRow.finished_at == None) # noqa: E711
            .values(
                finished_at=datetime.now(UTC),
                status="failed",
                errors_json=json.dumps({"error": "Interrupted by tracker restart"})
            )
        )
        await session.execute(stmt)
        await session.commit()

    scheduler = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})
    _state["scheduler"] = scheduler

    async def scheduled_ebay_run():
        try:
            await _trigger_ebay_run_logic()
        except Exception:
            logger.exception("Scheduled eBay run failed")

    scheduler.add_job(
        scheduled_ebay_run,
        trigger=IntervalTrigger(minutes=settings.scan_interval_minutes),
        id="ebay_scan",
        replace_existing=True,
    )

    scheduler.start()

    if settings.scan_on_startup:
        import asyncio
        asyncio.create_task(scheduled_ebay_run())

    yield

    scheduler.shutdown()

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    settings: Settings = _state.get("settings") or get_settings()
    expected_token = settings.api_token.get_secret_value()

    is_valid = secrets.compare_digest(
        credentials.credentials.encode("utf-8"),
        expected_token.encode("utf-8")
    )
    if not is_valid:
        raise HTTPException(status_code=401, detail="Invalid API token")
    return credentials

async def _trigger_ebay_run_logic() -> RunTriggerResponse:
    settings: Settings = _state["settings"]
    if not settings.ebay_client_id or not settings.ebay_client_secret:
        raise HTTPException(status_code=400, detail="eBay credentials not configured")

    session_factory = _state["session_factory"]
    matcher = PartMatcher(settings.parts_config_path)

    notifier = None
    if settings.telegram_bot_token and settings.telegram_chat_id:
        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

    alert_service = AlertService(notifier=notifier)
    search_service = SearchService(
        session_factory=session_factory,
        matcher=matcher,
        alert_service=alert_service,
        score_threshold=settings.minimum_match_score,
        price_change_percent=settings.price_drop_percent,
    )

    adapter = EbayAdapter(
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret,
        marketplace_id=settings.ebay_marketplace_id,
        timeout=settings.ebay_api_timeout,
        max_results_per_query=settings.ebay_max_results_per_query,
    )

    try:
        result = await search_service.run(adapter)
        return RunTriggerResponse(
            status="completed",
            message=f"eBay search completed with {result.total_found} listings",
            total_found=result.total_found,
            new_listings=result.new_listings,
            matches_found=result.matches_found,
            alerts_sent=result.alerts_sent,
        )
    finally:
        await adapter.close()
        if notifier:
            await notifier.close()

def create_app() -> FastAPI:
    app = FastAPI(
        title="Irmscher Parts Tracker",
        description="Track Irmscher parts for Opel Signum across marketplaces",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(version=__version__)

    @app.get("/listings", response_model=list[ListingResponse])
    async def list_listings(
        source: str | None = None,
        is_active: bool | None = None,
        max_price: float | None = None,
        limit: int = Query(default=50, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[ListingResponse]:
        repo = ListingRepository()
        session_factory = _state["session_factory"]
        async with session_factory() as session:
            rows = await repo.list_listings(
                session, source=source, is_active=is_active,
                limit=limit, offset=offset, max_price=max_price,
            )
            return [_listing_to_response(r) for r in rows]

    @app.get("/listings/{listing_id}", response_model=ListingResponse)
    async def get_listing(listing_id: int) -> ListingResponse:
        repo = ListingRepository()
        session_factory = _state["session_factory"]
        async with session_factory() as session:
            row = await repo.get_by_id(session, listing_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Listing not found")
            return _listing_to_response(row)

    @app.get("/matches", response_model=list[MatchResponse])
    async def list_matches(
        part_id: str | None = None,
        min_score: int | None = None,
        limit: int = Query(default=50, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[MatchResponse]:
        repo = MatchRepository()
        session_factory = _state["session_factory"]
        async with session_factory() as session:
            rows = await repo.list_matches(
                session, part_id=part_id, min_score=min_score,
                limit=limit, offset=offset,
            )
            return [_match_to_response(r) for r in rows]

    @app.get("/search-runs", response_model=list[SearchRunResponse])
    async def list_search_runs(
        limit: int = Query(default=20, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[SearchRunResponse]:
        repo = SearchRunRepository()
        session_factory = _state["session_factory"]
        async with session_factory() as session:
            rows = await repo.list_runs(session, limit=limit, offset=offset)
            return [_run_to_response(r) for r in rows]

    @app.post("/runs/ebay", response_model=RunTriggerResponse, dependencies=[Depends(verify_token)])
    async def trigger_ebay_run() -> RunTriggerResponse:
        return await _trigger_ebay_run_logic()

    return app

def _listing_to_response(row) -> ListingResponse:
    try:
        image_urls = json.loads(row.image_urls_json) if hasattr(row, 'image_urls_json') and row.image_urls_json else []
    except Exception:
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

def _match_to_response(row) -> MatchResponse:
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

def _run_to_response(row) -> SearchRunResponse:
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
