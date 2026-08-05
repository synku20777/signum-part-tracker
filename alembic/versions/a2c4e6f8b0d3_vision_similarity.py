"""Add CPU visual-similarity runs, embeddings, and evidence.

Revision ID: a2c4e6f8b0d3
Revises: f1a3c5e7b9d2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2c4e6f8b0d3"
down_revision: str | None = "f1a3c5e7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("manual_reviews") as batch:
        batch.drop_constraint("ck_manual_review_queue_mode", type_="check")
        batch.create_check_constraint(
            "ck_manual_review_queue_mode",
            "created_from_queue_mode IN ('legacy', 'api', 'all', "
            "'matched-high-confidence', 'matched-low-confidence', "
            "'unmatched-broad-candidates', 'confirmed-needs-positive-images', "
            "'part-needs-negatives', 'uncertain-recheck', 'visual-candidates')",
        )
    op.create_table(
        "vision_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("model_fingerprint", sa.String(64), nullable=True),
        sa.Column("requested_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "run_type IN ('warmup', 'reference_rebuild', 'listing_scan', 'evaluation')",
            name="ck_vision_run_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed', 'interrupted')",
            name="ck_vision_run_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vision_runs_run_type", "vision_runs", ["run_type"])
    op.create_index("ix_vision_runs_status", "vision_runs", ["status"])
    op.create_index(
        "uq_vision_single_running",
        "vision_runs",
        ["status"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "image_embeddings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_image_id", sa.Integer(), nullable=True),
        sa.Column("reference_image_id", sa.Integer(), nullable=True),
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("model_revision", sa.String(128), nullable=False),
        sa.Column("model_fingerprint", sa.String(64), nullable=False),
        sa.Column("preprocessing_version", sa.String(64), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("dtype", sa.String(16), nullable=False, server_default="float32"),
        sa.Column("vector_blob", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("owner_type IN ('listing', 'reference')", name="ck_embedding_owner"),
        sa.CheckConstraint("dtype = 'float32'", name="ck_embedding_dtype"),
        sa.CheckConstraint("embedding_dim > 0", name="ck_embedding_dim"),
        sa.CheckConstraint(
            "(owner_type = 'listing' AND listing_image_id IS NOT NULL AND "
            "reference_image_id IS NULL) OR "
            "(owner_type = 'reference' AND reference_image_id IS NOT NULL AND "
            "listing_image_id IS NULL)",
            name="ck_embedding_exactly_one_owner",
        ),
        sa.ForeignKeyConstraint(["listing_image_id"], ["listing_images.id"]),
        sa.ForeignKeyConstraint(["reference_image_id"], ["reference_images.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "listing_image_id",
        "reference_image_id",
        "content_sha256",
        "model_fingerprint",
    ):
        op.create_index(f"ix_image_embeddings_{column}", "image_embeddings", [column])
    op.create_index(
        "uq_embedding_listing_content_model",
        "image_embeddings",
        ["listing_image_id", "content_sha256", "model_fingerprint"],
        unique=True,
        sqlite_where=sa.text("owner_type = 'listing'"),
    )
    op.create_index(
        "uq_embedding_reference_content_model",
        "image_embeddings",
        ["reference_image_id", "content_sha256", "model_fingerprint"],
        unique=True,
        sqlite_where=sa.text("owner_type = 'reference'"),
    )

    op.create_table(
        "visual_matches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_image_id", sa.Integer(), nullable=False),
        sa.Column("part_id", sa.String(), nullable=False),
        sa.Column("model_fingerprint", sa.String(64), nullable=False),
        sa.Column("best_positive_reference_id", sa.Integer(), nullable=True),
        sa.Column("best_negative_reference_id", sa.Integer(), nullable=True),
        sa.Column("positive_similarity", sa.Float(), nullable=True),
        sa.Column("negative_similarity", sa.Float(), nullable=True),
        sa.Column("similarity_margin", sa.Float(), nullable=True),
        sa.Column("positive_reference_count", sa.Integer(), nullable=False),
        sa.Column("negative_reference_count", sa.Integer(), nullable=False),
        sa.Column("rank_for_listing", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ranked', 'review_candidate', 'positive_only', 'insufficient_references')",
            name="ck_visual_match_status",
        ),
        sa.CheckConstraint("rank_for_listing > 0", name="ck_visual_match_rank"),
        sa.ForeignKeyConstraint(["listing_image_id"], ["listing_images.id"]),
        sa.ForeignKeyConstraint(["best_positive_reference_id"], ["reference_images.id"]),
        sa.ForeignKeyConstraint(["best_negative_reference_id"], ["reference_images.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "listing_image_id",
            "part_id",
            "model_fingerprint",
            name="uq_visual_match_image_part_model",
        ),
    )
    for column in ("listing_image_id", "part_id", "model_fingerprint", "status"):
        op.create_index(f"ix_visual_matches_{column}", "visual_matches", [column])


def downgrade() -> None:
    op.drop_table("visual_matches")
    op.drop_table("image_embeddings")
    op.drop_table("vision_runs")
    with op.batch_alter_table("manual_reviews") as batch:
        batch.drop_constraint("ck_manual_review_queue_mode", type_="check")
        batch.create_check_constraint(
            "ck_manual_review_queue_mode",
            "created_from_queue_mode IN ('legacy', 'api', 'all', "
            "'matched-high-confidence', 'matched-low-confidence', "
            "'unmatched-broad-candidates', 'confirmed-needs-positive-images', "
            "'part-needs-negatives', 'uncertain-recheck')",
        )
