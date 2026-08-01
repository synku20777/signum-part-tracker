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
    seller: Mapped[str] = mapped_column(sa.String)
    seller_location: Mapped[str] = mapped_column(sa.String)
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
    seller: Mapped[str] = mapped_column(sa.String)
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
