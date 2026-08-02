"""Add eBay seller identity and deletion processing state.

Revision ID: b7d9e2f4a6c1
Revises: 9c1e4a7b2f60
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d9e2f4a6c1"
down_revision: str | None = "9c1e4a7b2f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings") as batch:
        batch.alter_column("seller", new_column_name="seller_display")
        batch.add_column(sa.Column("seller_identifier", sa.String(), nullable=True))
        batch.add_column(sa.Column("seller_identifier_type", sa.String(), nullable=True))
        batch.add_column(sa.Column("seller_feedback_score", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("seller_feedback_percentage", sa.Numeric(7, 4), nullable=True))
        batch.add_column(sa.Column("seller_anonymized_at", sa.DateTime(timezone=True)))

    with op.batch_alter_table("listing_snapshots") as batch:
        batch.alter_column("seller", new_column_name="seller_display")
        batch.add_column(sa.Column("seller_identifier", sa.String(), nullable=True))
        batch.add_column(sa.Column("seller_identifier_type", sa.String(), nullable=True))
        batch.add_column(sa.Column("seller_feedback_score", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("seller_feedback_percentage", sa.Numeric(7, 4), nullable=True))

    op.create_table(
        "ebay_deletion_notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("notification_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("eias_token", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'processed')",
            name="ck_ebay_deletion_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id", name="uq_ebay_deletion_notification_id"),
    )
    op.create_index(
        "ix_ebay_deletion_notifications_notification_id",
        "ebay_deletion_notifications",
        ["notification_id"],
        unique=True,
    )
    op.create_index(
        "ix_ebay_deletion_notifications_status",
        "ebay_deletion_notifications",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ebay_deletion_notifications_status",
        table_name="ebay_deletion_notifications",
    )
    op.drop_index(
        "ix_ebay_deletion_notifications_notification_id",
        table_name="ebay_deletion_notifications",
    )
    op.drop_table("ebay_deletion_notifications")

    with op.batch_alter_table("listing_snapshots") as batch:
        batch.drop_column("seller_feedback_percentage")
        batch.drop_column("seller_feedback_score")
        batch.drop_column("seller_identifier_type")
        batch.drop_column("seller_identifier")
        batch.alter_column("seller_display", new_column_name="seller")

    with op.batch_alter_table("listings") as batch:
        batch.drop_column("seller_anonymized_at")
        batch.drop_column("seller_feedback_percentage")
        batch.drop_column("seller_feedback_score")
        batch.drop_column("seller_identifier_type")
        batch.drop_column("seller_identifier")
        batch.alter_column("seller_display", new_column_name="seller")
