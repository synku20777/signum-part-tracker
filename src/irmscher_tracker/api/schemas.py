from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str

class ListingResponse(BaseModel):
    id: int
    source: str
    external_id: str
    title: str
    description: str
    url: str
    image_urls: list[str]
    price: Decimal
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
    # Include listing info
    listing_title: str | None = None
    listing_url: str | None = None
    listing_price: Decimal | None = None

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

class RunTriggerResponse(BaseModel):
    status: str
    message: str
    total_found: int = 0
    new_listings: int = 0
    matches_found: int = 0
    alerts_sent: int = 0

class PaginatedResponse(BaseModel):
    items: list
    total: int
    limit: int
    offset: int
