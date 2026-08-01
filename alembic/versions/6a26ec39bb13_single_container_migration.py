"""Add SQLite lifecycle, snapshot, query, and notification state.

Revision ID: 6a26ec39bb13
Revises: 001
"""

import hashlib
import json
import re
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "6a26ec39bb13"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _money(value: object | None) -> str | None:
    return str(Decimal(str(value)).quantize(Decimal("0.01"))) if value is not None else None


def _normalized_text(value: object | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def upgrade() -> None:
    op.create_table(
        "listing_queries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("query", sa.String(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id", "source", "query", name="uq_listing_query"),
    )
    op.create_index("ix_listing_queries_listing_id", "listing_queries", ["listing_id"])
    op.create_index("ix_listing_queries_source", "listing_queries", ["source"])

    with op.batch_alter_table("listing_snapshots") as batch:
        batch.add_column(sa.Column("schema_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("payload_hash", sa.String(), nullable=True))
        batch.add_column(sa.Column("image_urls_json", sa.Text(), nullable=True))
    with op.batch_alter_table("listings") as batch:
        batch.add_column(sa.Column("image_urls_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("last_changed_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("inactive_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("reactivated_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("consecutive_misses", sa.Integer(), nullable=True))
    with op.batch_alter_table("notifications") as batch:
        batch.add_column(sa.Column("event_key", sa.String(), nullable=True))
    with op.batch_alter_table("part_matches") as batch:
        batch.add_column(sa.Column("compatibility_status", sa.String(), nullable=True))

    connection = op.get_bind()
    listing_rows = connection.execute(sa.text("SELECT id, image_url FROM listings")).mappings()
    for row in listing_rows:
        images = [row["image_url"]] if row["image_url"] else []
        connection.execute(
            sa.text(
                "UPDATE listings SET image_urls_json=:images, consecutive_misses=0 WHERE id=:id"
            ),
            {"id": row["id"], "images": json.dumps(images)},
        )

    snapshot_rows = connection.execute(
        sa.text(
            "SELECT s.*, l.url AS listing_url FROM listing_snapshots s "
            "JOIN listings l ON l.id=s.listing_id"
        )
    ).mappings()
    for row in snapshot_rows:
        images = [row["image_url"]] if row["image_url"] else []
        payload = {
            "schema_version": 1,
            "title": _normalized_text(row["title"]),
            "description": _normalized_text(row["description"]),
            "price": _money(row["price"]),
            "currency": str(row["currency"]).upper(),
            "shipping_cost": _money(row["shipping_cost"]),
            "condition": row["condition"],
            "seller": str(row["seller"] or "").strip(),
            "seller_location": str(row["seller_location"] or "").strip(),
            "image_urls": sorted(images),
            "url": str(row["listing_url"] or "").strip(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        connection.execute(
            sa.text(
                "UPDATE listing_snapshots SET schema_version=1, payload_hash=:hash, "
                "image_urls_json=:images WHERE id=:id"
            ),
            {
                "id": row["id"],
                "hash": hashlib.sha256(encoded.encode()).hexdigest(),
                "images": json.dumps(images),
            },
        )

    connection.execute(sa.text("UPDATE notifications SET event_key='legacy:' || id"))
    connection.execute(sa.text("UPDATE part_matches SET compatibility_status='unknown'"))

    duplicate_groups = connection.execute(
        sa.text(
            "SELECT listing_id, MAX(id) AS keep_id FROM part_matches "
            "GROUP BY listing_id HAVING COUNT(*) > 1"
        )
    ).mappings()
    for group in duplicate_groups:
        duplicate_ids = connection.execute(
            sa.text("SELECT id FROM part_matches WHERE listing_id=:listing_id AND id<>:keep_id"),
            group,
        ).scalars()
        for duplicate_id in duplicate_ids:
            connection.execute(
                sa.text("UPDATE notifications SET match_id=:keep_id WHERE match_id=:old_id"),
                {"keep_id": group["keep_id"], "old_id": duplicate_id},
            )
            connection.execute(
                sa.text("DELETE FROM part_matches WHERE id=:id"), {"id": duplicate_id}
            )

    with op.batch_alter_table("listing_snapshots") as batch:
        batch.alter_column("schema_version", nullable=False, server_default="1")
        batch.alter_column("payload_hash", nullable=False)
        batch.alter_column("image_urls_json", nullable=False, server_default="[]")
        batch.drop_column("image_url")
    op.create_index("ix_listing_snapshots_payload_hash", "listing_snapshots", ["payload_hash"])
    with op.batch_alter_table("listings") as batch:
        batch.alter_column("image_urls_json", nullable=False, server_default="[]")
        batch.alter_column("consecutive_misses", nullable=False, server_default="0")
        batch.drop_column("image_url")
    with op.batch_alter_table("notifications") as batch:
        batch.alter_column("event_key", nullable=False)
        batch.create_unique_constraint("uq_notification_event_key", ["event_key"])
    op.create_index("ix_notifications_event_key", "notifications", ["event_key"])
    with op.batch_alter_table("part_matches") as batch:
        batch.alter_column("compatibility_status", nullable=False)
        batch.create_unique_constraint("uq_match_listing", ["listing_id"])


def downgrade() -> None:
    with op.batch_alter_table("part_matches") as batch:
        batch.drop_constraint("uq_match_listing", type_="unique")
        batch.drop_column("compatibility_status")
    op.drop_index("ix_notifications_event_key", table_name="notifications")
    with op.batch_alter_table("notifications") as batch:
        batch.drop_constraint("uq_notification_event_key", type_="unique")
        batch.drop_column("event_key")
    with op.batch_alter_table("listings") as batch:
        batch.add_column(sa.Column("image_url", sa.String()))
    with op.batch_alter_table("listing_snapshots") as batch:
        batch.add_column(sa.Column("image_url", sa.String()))

    connection = op.get_bind()
    for table in ("listings", "listing_snapshots"):
        rows = connection.execute(sa.text(f"SELECT id, image_urls_json FROM {table}")).mappings()
        for row in rows:
            images = json.loads(row["image_urls_json"] or "[]")
            connection.execute(
                sa.text(f"UPDATE {table} SET image_url=:image WHERE id=:id"),
                {"id": row["id"], "image": images[0] if images else None},
            )

    with op.batch_alter_table("listings") as batch:
        batch.drop_column("consecutive_misses")
        batch.drop_column("reactivated_at")
        batch.drop_column("inactive_at")
        batch.drop_column("last_changed_at")
        batch.drop_column("image_urls_json")
    op.drop_index("ix_listing_snapshots_payload_hash", table_name="listing_snapshots")
    with op.batch_alter_table("listing_snapshots") as batch:
        batch.drop_column("image_urls_json")
        batch.drop_column("payload_hash")
        batch.drop_column("schema_version")
    op.drop_index("ix_listing_queries_source", table_name="listing_queries")
    op.drop_index("ix_listing_queries_listing_id", table_name="listing_queries")
    op.drop_table("listing_queries")
