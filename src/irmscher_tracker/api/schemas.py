from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class ReviewPartResponse(BaseModel):
    id: str
    name: str


class ListingImageResponse(BaseModel):
    id: int
    source_url: str
    position: int
    is_current: bool
    first_seen_at: datetime
    last_seen_at: datetime


class ManualReviewResponse(BaseModel):
    id: int
    listing_id: int
    outcome: Literal["confirmed", "rejected", "uncertain"]
    selected_part_id: str | None
    notes: str | None
    reviewed_at: datetime


class ReviewMatchResponse(BaseModel):
    part_id: str
    part_name: str
    total_score: int
    compatibility_status: str
    reasons: list[dict[str, object]]
    algorithm_version: str


class ReviewQueueItemResponse(BaseModel):
    listing_id: int
    source: str
    title: str
    description: str
    url: str
    price: Decimal | None
    currency: str
    condition: str
    published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool
    images: list[ListingImageResponse]
    deterministic_match: ReviewMatchResponse | None
    effective_part_id: str | None
    latest_review: ManualReviewResponse | None
    review_history_count: int


class ReviewQueueResponse(BaseModel):
    items: list[ReviewQueueItemResponse]
    total: int
    limit: int
    offset: int


class ReviewOutcomeProgress(BaseModel):
    confirmed: int
    rejected: int
    uncertain: int


class ReviewQueueProgress(BaseModel):
    unreviewed_matched: int
    unreviewed_unmatched: int


class ReviewSourceProgress(BaseModel):
    source: str
    reviewed_listings: int


class ReviewPartProgress(BaseModel):
    part_id: str
    part_name: str
    confirmed_listings: int
    positive_references: int
    positive_listings: int
    negative_references: int
    negative_listings: int
    missing_requirements: list[str]
    coverage_ready: bool


class ReviewProgressResponse(BaseModel):
    target_reviews: int
    reviewed_listings: int
    remaining_reviews: int
    campaign_complete: bool
    coverage_complete: bool
    outcomes: ReviewOutcomeProgress
    queue: ReviewQueueProgress
    sources: list[ReviewSourceProgress]
    parts: list[ReviewPartProgress]


class ReferenceImageResponse(BaseModel):
    id: int
    listing_id: int
    listing_image_id: int
    manual_review_id: int
    part_id: str
    label: Literal["positive", "negative"]
    content_sha256: str
    mime_type: str
    width: int
    height: int
    notes: str | None
    is_active: bool
    created_at: datetime
    content_url: str


class ReviewListingDetailResponse(ReviewQueueItemResponse):
    images: list[ListingImageResponse]
    review_history: list[ManualReviewResponse]
    references: list[ReferenceImageResponse]


class ReferenceSelection(BaseModel):
    listing_image_id: int
    label: Literal["positive", "negative"]
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class ManualReviewRequest(BaseModel):
    outcome: Literal["confirmed", "rejected", "uncertain"]
    selected_part_id: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    references: list[ReferenceSelection] = Field(default_factory=list)

    @field_validator("selected_part_id", "notes")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def validate_reference_labels(self) -> ManualReviewRequest:
        if self.outcome == "confirmed" and not self.selected_part_id:
            raise ValueError("Confirmed reviews require a selected part")
        if self.references and not self.selected_part_id:
            raise ValueError("Reference images require a selected part")
        if self.outcome != "confirmed" and any(
            reference.label == "positive" for reference in self.references
        ):
            raise ValueError("Positive references require a confirmed review")
        image_ids = [reference.listing_image_id for reference in self.references]
        if len(image_ids) != len(set(image_ids)):
            raise ValueError("An image can be selected only once per review")
        return self


class ReferenceResultResponse(BaseModel):
    listing_image_id: int
    status: Literal["created", "existing", "reactivated", "failed", "conflict"]
    reference: ReferenceImageResponse | None = None
    detail: str | None = None


class ManualReviewCreatedResponse(BaseModel):
    review: ManualReviewResponse
    references: list[ReferenceResultResponse]


class ReferenceUpdateRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def require_update(self) -> ReferenceUpdateRequest:
        if "notes" not in self.model_fields_set and self.is_active is None:
            raise ValueError("At least one field is required")
        return self
