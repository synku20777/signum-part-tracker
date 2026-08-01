"""Add SS.com refresh state and nullable prices.

Revision ID: 9c1e4a7b2f60
Revises: 6a26ec39bb13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c1e4a7b2f60"
down_revision: str | None = "6a26ec39bb13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings") as batch:
        batch.alter_column("price", existing_type=sa.Numeric(12, 2), nullable=True)
        batch.add_column(
            sa.Column(
                "source_metadata_json",
                sa.Text(),
                nullable=False,
                server_default='{"schema_version":1}',
            )
        )
        batch.add_column(sa.Column("rss_fingerprint_seen", sa.String(), nullable=True))
        batch.add_column(sa.Column("rss_fingerprint_enriched", sa.String(), nullable=True))
        batch.add_column(sa.Column("last_detail_success_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column(
                "detail_status",
                sa.String(),
                nullable=False,
                server_default="not_applicable",
            )
        )
    with op.batch_alter_table("listing_snapshots") as batch:
        batch.alter_column("price", existing_type=sa.Numeric(12, 2), nullable=True)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE listings SET price=0 WHERE price IS NULL"))
    connection.execute(sa.text("UPDATE listing_snapshots SET price=0 WHERE price IS NULL"))
    with op.batch_alter_table("listing_snapshots") as batch:
        batch.alter_column("price", existing_type=sa.Numeric(12, 2), nullable=False)
    with op.batch_alter_table("listings") as batch:
        batch.drop_column("detail_status")
        batch.drop_column("last_detail_success_at")
        batch.drop_column("rss_fingerprint_enriched")
        batch.drop_column("rss_fingerprint_seen")
        batch.drop_column("source_metadata_json")
        batch.alter_column("price", existing_type=sa.Numeric(12, 2), nullable=False)
