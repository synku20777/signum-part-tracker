"""Add review provenance and reference quality metadata.

Revision ID: f1a3c5e7b9d2
Revises: d4f8a2c6e9b1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a3c5e7b9d2"
down_revision: str | None = "d4f8a2c6e9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_QUEUE_MODES = (
    "legacy",
    "api",
    "all",
    "matched-high-confidence",
    "matched-low-confidence",
    "unmatched-broad-candidates",
    "confirmed-needs-positive-images",
    "part-needs-negatives",
    "uncertain-recheck",
)
_DECISION_REASONS = (
    "exact-visible-part-number",
    "visual-shape-match",
    "catalogue-comparison",
    "known-donor-car",
    "wrong-part",
    "wrong-model",
    "pre-facelift",
    "replica",
    "ordinary-OEM-part",
    "image-does-not-show-part",
    "listing-no-longer-available",
    "insufficient-angle",
    "low-resolution",
    "obstructed",
    "conflicting-evidence",
    "other",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(repr(value) for value in values)


def upgrade() -> None:
    with op.batch_alter_table("manual_reviews") as batch:
        batch.add_column(sa.Column("previous_review_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("reviewer_version", sa.String(32), nullable=True))
        batch.add_column(sa.Column("review_ui_version", sa.String(32), nullable=True))
        batch.add_column(sa.Column("decision_reason", sa.String(64), nullable=True))
        batch.add_column(sa.Column("created_from_queue_mode", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_manual_reviews_previous_review",
            "manual_reviews",
            ["previous_review_id"],
            ["id"],
        )
        batch.create_check_constraint(
            "ck_manual_review_decision_reason",
            f"decision_reason IS NULL OR decision_reason IN ({_quoted(_DECISION_REASONS)})",
        )
        batch.create_check_constraint(
            "ck_manual_review_queue_mode",
            f"created_from_queue_mode IN ({_quoted(_QUEUE_MODES)})",
        )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, listing_id FROM manual_reviews ORDER BY listing_id, reviewed_at, id")
    )
    previous_by_listing: dict[int, int] = {}
    for review_id, listing_id in rows:
        connection.execute(
            sa.text(
                "UPDATE manual_reviews SET previous_review_id=:previous_id, "
                "reviewer_version='legacy', review_ui_version='legacy', "
                "created_from_queue_mode='legacy' WHERE id=:review_id"
            ),
            {
                "previous_id": previous_by_listing.get(listing_id),
                "review_id": review_id,
            },
        )
        previous_by_listing[listing_id] = review_id

    with op.batch_alter_table("manual_reviews") as batch:
        batch.alter_column("reviewer_version", existing_type=sa.String(32), nullable=False)
        batch.alter_column("review_ui_version", existing_type=sa.String(32), nullable=False)
        batch.alter_column("created_from_queue_mode", existing_type=sa.String(64), nullable=False)

    with op.batch_alter_table("reference_images") as batch:
        batch.add_column(sa.Column("view", sa.String(32), nullable=True))
        batch.add_column(sa.Column("context", sa.String(16), nullable=True))
        batch.add_column(sa.Column("quality", sa.String(16), nullable=True))
        batch.add_column(sa.Column("obstruction", sa.String(16), nullable=True))
        batch.add_column(
            sa.Column("privacy_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_check_constraint(
            "ck_reference_view",
            "view IS NULL OR view IN ('front', 'rear', 'left', 'right', "
            "'front-three-quarter', 'rear-three-quarter', 'top', 'underside', "
            "'detail', 'unknown')",
        )
        batch.create_check_constraint(
            "ck_reference_context",
            "context IS NULL OR context IN "
            "('fitted', 'removed', 'catalogue', 'packaging', 'unknown')",
        )
        batch.create_check_constraint(
            "ck_reference_quality",
            "quality IS NULL OR quality IN ('good', 'usable', 'poor')",
        )
        batch.create_check_constraint(
            "ck_reference_obstruction",
            "obstruction IS NULL OR obstruction IN ('none', 'partial', 'severe')",
        )


def downgrade() -> None:
    with op.batch_alter_table("reference_images") as batch:
        batch.drop_constraint("ck_reference_obstruction", type_="check")
        batch.drop_constraint("ck_reference_quality", type_="check")
        batch.drop_constraint("ck_reference_context", type_="check")
        batch.drop_constraint("ck_reference_view", type_="check")
        batch.drop_column("privacy_checked_at")
        batch.drop_column("obstruction")
        batch.drop_column("quality")
        batch.drop_column("context")
        batch.drop_column("view")
    with op.batch_alter_table("manual_reviews") as batch:
        batch.drop_constraint("ck_manual_review_queue_mode", type_="check")
        batch.drop_constraint("ck_manual_review_decision_reason", type_="check")
        batch.drop_constraint("fk_manual_reviews_previous_review", type_="foreignkey")
        batch.drop_column("created_from_queue_mode")
        batch.drop_column("decision_reason")
        batch.drop_column("review_ui_version")
        batch.drop_column("reviewer_version")
        batch.drop_column("previous_review_id")
