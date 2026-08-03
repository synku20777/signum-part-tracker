from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ListingRow(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(sa.String, index=True)
    external_id: Mapped[str] = mapped_column(sa.String)
    title: Mapped[str] = mapped_column(sa.String)
    description: Mapped[str] = mapped_column(sa.Text)
    url: Mapped[str] = mapped_column(sa.String)
    image_urls_json: Mapped[str] = mapped_column(sa.Text, default="[]")
    price: Mapped[Decimal | None] = mapped_column(sa.Numeric(precision=12, scale=2))
    currency: Mapped[str] = mapped_column(sa.String)
    shipping_cost: Mapped[Decimal | None] = mapped_column(sa.Numeric(precision=12, scale=2))
    condition: Mapped[str] = mapped_column(sa.String)
    seller_display: Mapped[str] = mapped_column(sa.String)
    seller_identifier: Mapped[str | None] = mapped_column(sa.String)
    seller_identifier_type: Mapped[str | None] = mapped_column(sa.String)
    seller_feedback_score: Mapped[int | None] = mapped_column(sa.Integer)
    seller_feedback_percentage: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(precision=7, scale=4)
    )
    seller_location: Mapped[str] = mapped_column(sa.String)
    seller_anonymized_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    published_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    inactive_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    reactivated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    source_metadata_json: Mapped[str] = mapped_column(sa.Text, default='{"schema_version":1}')
    rss_fingerprint_seen: Mapped[str | None] = mapped_column(sa.String)
    rss_fingerprint_enriched: Mapped[str | None] = mapped_column(sa.String)
    last_detail_success_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    detail_status: Mapped[str] = mapped_column(sa.String, default="not_applicable")
    consecutive_misses: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, index=True)

    __table_args__ = (
        sa.UniqueConstraint("source", "external_id", name="uq_listing_source_ext_id"),
    )


class ListingQueryRow(Base):
    __tablename__ = "listing_queries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(sa.ForeignKey("listings.id"), index=True)
    source: Mapped[str] = mapped_column(sa.String, index=True)
    query: Mapped[str] = mapped_column(sa.String)
    first_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("listing_id", "source", "query", name="uq_listing_query"),
    )


class ListingSnapshotRow(Base):
    __tablename__ = "listing_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(sa.ForeignKey("listings.id"), index=True)
    schema_version: Mapped[int] = mapped_column(sa.Integer, default=1)
    payload_hash: Mapped[str] = mapped_column(sa.String, index=True)
    title: Mapped[str] = mapped_column(sa.String)
    description: Mapped[str] = mapped_column(sa.Text)
    price: Mapped[Decimal | None] = mapped_column(sa.Numeric(precision=12, scale=2))
    currency: Mapped[str] = mapped_column(sa.String)
    shipping_cost: Mapped[Decimal | None] = mapped_column(sa.Numeric(precision=12, scale=2))
    condition: Mapped[str] = mapped_column(sa.String)
    seller_display: Mapped[str] = mapped_column(sa.String)
    seller_identifier: Mapped[str | None] = mapped_column(sa.String)
    seller_identifier_type: Mapped[str | None] = mapped_column(sa.String)
    seller_feedback_score: Mapped[int | None] = mapped_column(sa.Integer)
    seller_feedback_percentage: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(precision=7, scale=4)
    )
    seller_location: Mapped[str] = mapped_column(sa.String)
    image_urls_json: Mapped[str] = mapped_column(sa.Text)
    captured_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class PartMatchRow(Base):
    __tablename__ = "part_matches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(sa.ForeignKey("listings.id"), index=True)
    part_id: Mapped[str] = mapped_column(sa.String, index=True)
    part_name: Mapped[str] = mapped_column(sa.String)
    total_score: Mapped[int] = mapped_column(sa.Integer)
    compatibility_status: Mapped[str] = mapped_column(sa.String)
    reasons_json: Mapped[str] = mapped_column(sa.Text)
    algorithm_version: Mapped[str] = mapped_column(sa.String)
    matched_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (sa.UniqueConstraint("listing_id", name="uq_match_listing"),)


class NotificationRow(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(sa.String, unique=True, index=True)
    listing_id: Mapped[int] = mapped_column(sa.ForeignKey("listings.id"), index=True)
    match_id: Mapped[int | None] = mapped_column(sa.ForeignKey("part_matches.id"))
    alert_type: Mapped[str] = mapped_column(sa.String)
    payload_json: Mapped[str] = mapped_column(sa.Text)
    sent_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    success: Mapped[bool] = mapped_column(sa.Boolean)
    error_message: Mapped[str | None] = mapped_column(sa.String)


class SearchRunRow(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(sa.String)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    total_found: Mapped[int] = mapped_column(sa.Integer)
    new_listings: Mapped[int] = mapped_column(sa.Integer)
    updated_listings: Mapped[int] = mapped_column(sa.Integer)
    matches_found: Mapped[int] = mapped_column(sa.Integer)
    alerts_sent: Mapped[int] = mapped_column(sa.Integer)
    errors_json: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String)


class EbayDeletionNotificationRow(Base):
    __tablename__ = "ebay_deletion_notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    notification_id: Mapped[str] = mapped_column(sa.String, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(sa.String)
    user_id: Mapped[str | None] = mapped_column(sa.String)
    eias_token: Mapped[str | None] = mapped_column(sa.String)
    status: Mapped[str] = mapped_column(sa.String, default="pending", index=True)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(sa.String)

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'processed')",
            name="ck_ebay_deletion_status",
        ),
    )


class ListingImageRow(Base):
    __tablename__ = "listing_images"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(sa.ForeignKey("listings.id"), index=True)
    source_url: Mapped[str] = mapped_column(sa.Text)
    position: Mapped[int] = mapped_column(sa.Integer)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("listing_id", "source_url", name="uq_listing_image_url"),
    )


class ManualReviewRow(Base):
    __tablename__ = "manual_reviews"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(sa.ForeignKey("listings.id"), index=True)
    outcome: Mapped[str] = mapped_column(sa.String)
    selected_part_id: Mapped[str | None] = mapped_column(sa.String)
    notes: Mapped[str | None] = mapped_column(sa.Text)
    reviewed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    previous_review_id: Mapped[int | None] = mapped_column(sa.ForeignKey("manual_reviews.id"))
    reviewer_version: Mapped[str] = mapped_column(sa.String(32), default="legacy")
    review_ui_version: Mapped[str] = mapped_column(sa.String(32), default="legacy")
    decision_reason: Mapped[str | None] = mapped_column(sa.String(64))
    created_from_queue_mode: Mapped[str] = mapped_column(sa.String(64), default="legacy")

    __table_args__ = (
        sa.CheckConstraint(
            "outcome IN ('confirmed', 'rejected', 'uncertain')",
            name="ck_manual_review_outcome",
        ),
    )


class ReferenceImageRow(Base):
    __tablename__ = "reference_images"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_image_id: Mapped[int] = mapped_column(sa.ForeignKey("listing_images.id"))
    manual_review_id: Mapped[int] = mapped_column(sa.ForeignKey("manual_reviews.id"))
    part_id: Mapped[str] = mapped_column(sa.String, index=True)
    label: Mapped[str] = mapped_column(sa.String)
    local_path: Mapped[str] = mapped_column(sa.Text)
    content_sha256: Mapped[str] = mapped_column(sa.String(64))
    mime_type: Mapped[str] = mapped_column(sa.String)
    width: Mapped[int] = mapped_column(sa.Integer)
    height: Mapped[int] = mapped_column(sa.Integer)
    notes: Mapped[str | None] = mapped_column(sa.Text)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    view: Mapped[str | None] = mapped_column(sa.String(32))
    context: Mapped[str | None] = mapped_column(sa.String(16))
    quality: Mapped[str | None] = mapped_column(sa.String(16))
    obstruction: Mapped[str | None] = mapped_column(sa.String(16))
    privacy_checked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint("label IN ('positive', 'negative')", name="ck_reference_label"),
        sa.UniqueConstraint(
            "part_id", "label", "content_sha256", name="uq_reference_part_label_content"
        ),
        sa.Index(
            "uq_reference_active_part_content",
            "part_id",
            "content_sha256",
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
        ),
    )
