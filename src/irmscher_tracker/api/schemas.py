from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: Literal["ok", "error"]
    scheduler: Literal["running", "stopped"]
    ebay_configured: bool
    sscom_configured: bool
    telegram_configured: bool


class ListingResponse(BaseModel):
    id: int
    source: str
    external_id: str
    title: str
    description: str
    url: str
    image_urls: list[str]
    price: Decimal | None
    currency: str
    shipping_cost: Decimal | None
    condition: str
    seller: str
    seller_location: str
    published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    last_changed_at: datetime | None
    inactive_at: datetime | None
    consecutive_misses: int
    is_active: bool


class MatchResponse(BaseModel):
    id: int
    listing_id: int
    part_id: str
    part_name: str
    total_score: int
    compatibility_status: str
    reasons_json: str
    algorithm_version: str
    matched_at: datetime


class SearchRunResponse(BaseModel):
    id: int
    source: str
    started_at: datetime
    finished_at: datetime | None
    total_found: int
    new_listings: int
    updated_listings: int
    matches_found: int
    alerts_sent: int
    status: str


class RunAcceptedResponse(BaseModel):
    search_run_id: int
    status: str
