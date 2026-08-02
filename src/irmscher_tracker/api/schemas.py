from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: Literal["ok", "error"]
    scheduler: Literal["running", "stopped"]
    ebay_configured: bool
    sscom_configured: bool
    telegram_configured: bool
    ebay_environment: Literal["sandbox", "production"]
    ebay_deletion_callback_configured: bool
    ebay_deletion_worker: Literal["disabled", "running", "stopped"]
    ebay_deletion_pending: int
    ebay_deletion_oldest_pending_seconds: float | None


class EbayDeletionMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    topic: Literal["MARKETPLACE_ACCOUNT_DELETION"]
    schemaVersion: str = Field(min_length=1, max_length=20)
    deprecated: bool


class EbayDeletionData(BaseModel):
    model_config = ConfigDict(extra="allow")

    username: str | None = Field(default=None, max_length=512)
    userId: str | None = Field(default=None, max_length=512)
    eiasToken: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def require_identifier(self) -> EbayDeletionData:
        identifiers = (self.username, self.userId, self.eiasToken)
        if not any(value and value.strip() for value in identifiers):
            raise ValueError("At least one seller identifier is required")
        return self


class EbayDeletionNotification(BaseModel):
    model_config = ConfigDict(extra="allow")

    notificationId: str = Field(min_length=1, max_length=256)
    eventDate: datetime
    publishDate: datetime
    publishAttemptCount: int = Field(ge=1)
    data: EbayDeletionData

    @model_validator(mode="after")
    def require_utc_dates(self) -> EbayDeletionNotification:
        for value in (self.eventDate, self.publishDate):
            offset = value.utcoffset()
            if offset is None or offset.total_seconds() != 0:
                raise ValueError("eBay deletion timestamps must be UTC")
        return self


class EbayDeletionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    metadata: EbayDeletionMetadata
    notification: EbayDeletionNotification


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
