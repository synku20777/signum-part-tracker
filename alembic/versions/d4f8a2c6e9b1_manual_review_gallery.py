"""Add manual reviews and normalized listing/reference images.

Revision ID: d4f8a2c6e9b1
Revises: b7d9e2f4a6c1
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8a2c6e9b1"
down_revision: str | None = "b7d9e2f4a6c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _valid_url(value: object) -> str | None:
    if not isinstance(value, str) or not (value := value.strip()):
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.hostname else None


def upgrade() -> None:
    op.create_table(
        "listing_images",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id", "source_url", name="uq_listing_image_url"),
    )
    op.create_index("ix_listing_images_listing_id", "listing_images", ["listing_id"])
    op.create_table(
        "manual_reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("selected_part_id", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('confirmed', 'rejected', 'uncertain')",
            name="ck_manual_review_outcome",
        ),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_manual_reviews_listing_id", "manual_reviews", ["listing_id"])
    op.create_table(
        "reference_images",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_image_id", sa.Integer(), nullable=False),
        sa.Column("manual_review_id", sa.Integer(), nullable=False),
        sa.Column("part_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("label IN ('positive', 'negative')", name="ck_reference_label"),
        sa.ForeignKeyConstraint(["listing_image_id"], ["listing_images.id"]),
        sa.ForeignKeyConstraint(["manual_review_id"], ["manual_reviews.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "part_id", "label", "content_sha256", name="uq_reference_part_label_content"
        ),
    )
    op.create_index("ix_reference_images_part_id", "reference_images", ["part_id"])
    op.create_index(
        "uq_reference_active_part_content",
        "reference_images",
        ["part_id", "content_sha256"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

    connection = op.get_bind()
    listings = connection.execute(
        sa.text(
            "SELECT id, image_urls_json, first_seen_at, last_seen_at FROM listings ORDER BY id"
        )
    )
    for listing_id, raw_images, first_seen, last_seen in listings:
        try:
            values = json.loads(raw_images or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(values, list):
            continue
        seen: set[str] = set()
        for value in values:
            url = _valid_url(value)
            if url is None or url in seen:
                continue
            seen.add(url)
            connection.execute(
                sa.text(
                    "INSERT INTO listing_images "
                    "(listing_id, source_url, position, is_current, first_seen_at, last_seen_at) "
                    "VALUES (:listing_id, :source_url, :position, 1, :first_seen, :last_seen)"
                ),
                {
                    "listing_id": listing_id,
                    "source_url": url,
                    "position": len(seen) - 1,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                },
            )


def downgrade() -> None:
    op.drop_index("uq_reference_active_part_content", table_name="reference_images")
    op.drop_index("ix_reference_images_part_id", table_name="reference_images")
    op.drop_table("reference_images")
    op.drop_index("ix_manual_reviews_listing_id", table_name="manual_reviews")
    op.drop_table("manual_reviews")
    op.drop_index("ix_listing_images_listing_id", table_name="listing_images")
    op.drop_table("listing_images")
