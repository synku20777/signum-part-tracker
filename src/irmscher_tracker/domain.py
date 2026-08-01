"""Pydantic domain models for the Irmscher Parts Tracker.

These models define the canonical shapes used throughout the application.
Marketplace-specific structures are never exposed beyond their adapters.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Source(StrEnum):
    """Supported marketplace data sources."""

    EBAY = "ebay"
    KLEINANZEIGEN = "kleinanzeigen"
    ALLEGRO = "allegro"
    OVOKO = "ovoko"
    SSCOM = "sscom"


class ListingCondition(StrEnum):
    """Possible conditions of a listing item."""

    NEW = "new"
    USED = "used"
    PARTS_ONLY = "parts_only"
    REFURBISHED = "refurbished"
    UNKNOWN = "unknown"


class NormalizedListing(BaseModel):
    """A marketplace-independent representation of a part listing."""

    source: Source
    external_id: str
    title: str
    description: str = ""
    url: str
    image_urls: list[str] = Field(default_factory=list)
    price: Decimal | None
    currency: str = "EUR"
    shipping_cost: Decimal | None = None
    condition: ListingCondition = ListingCondition.UNKNOWN
    seller: str = ""
    seller_location: str = ""
    published_at: datetime | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict, exclude=True)
    rss_fingerprint_seen: str | None = Field(default=None, exclude=True)
    rss_fingerprint_enriched: str | None = Field(default=None, exclude=True)
    last_detail_success_at: datetime | None = Field(default=None, exclude=True)
    detail_status: str = Field(default="not_applicable", exclude=True)
    raw_data: dict[str, Any] = Field(default_factory=dict, exclude=True)


class ScoringReason(BaseModel):
    """One rule contribution to a match score."""

    rule: str
    points: int
    detail: str = ""


class MatchResult(BaseModel):
    """Result of scoring a listing against a part definition."""

    part_id: str
    part_name: str
    total_score: int
    compatibility_status: str
    reasons: list[ScoringReason]
    has_part_specific_evidence: bool = False
    algorithm_version: str = "1.0"


class AlertType(StrEnum):
    """Types of alerts the tracker can emit."""

    NEW_LISTING = "new_listing"
    SCORE_THRESHOLD_CROSSED = "score_threshold_crossed"
    PRICE_DECREASE = "price_decrease"
    REACTIVATED = "reactivated"


class AlertPayload(BaseModel):
    """Data included in an outgoing alert notification."""

    alert_type: AlertType
    listing_title: str
    listing_url: str
    source: Source
    part_id: str
    part_name: str
    score: int
    score_explanation: list[ScoringReason]
    price: Decimal | None
    currency: str = "EUR"
    shipping_cost: Decimal | None = None
    seller_location: str = ""
    previous_price: Decimal | None = None


class PartDefinition(BaseModel):
    """A single Irmscher part to watch for."""

    id: str
    name: str
    part_numbers: list[str]
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    compatible_models: list[str] = Field(default_factory=list)
    notes: str = ""


class NegativeRules(BaseModel):
    """Criteria that should exclude listings from matching."""

    excluded_part_numbers: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    incompatible_models: list[str] = Field(default_factory=list)


class PartsConfig(BaseModel):
    """Top-level container for the parts watchlist YAML."""

    parts: list[PartDefinition]
    negative_rules: NegativeRules = Field(default_factory=NegativeRules)


class SearchHit(BaseModel):
    """One normalized listing and every query that discovered it."""

    listing: NormalizedListing
    queries: set[str] = Field(default_factory=set)


class SourceSearchResult(BaseModel):
    """Marketplace search output with completeness metadata."""

    hits: list[SearchHit] = Field(default_factory=list)
    successful_queries: list[str] = Field(default_factory=list)
    query_errors: dict[str, str] = Field(default_factory=dict)
    discovery_complete: bool = True
    enrichment_complete: bool = True


class SearchRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class SearchRunResult(BaseModel):
    """Summary statistics for one search cycle."""

    source: Source
    run_id: int | None = None
    status: SearchRunStatus = SearchRunStatus.RUNNING
    total_found: int = 0
    new_listings: int = 0
    updated_listings: int = 0
    matches_found: int = 0
    alerts_sent: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
