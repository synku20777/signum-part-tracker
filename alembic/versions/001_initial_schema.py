"""Initial schema

Revision ID: 001
Revises: 
Create Date: 2024-05-18 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # listings table
    op.create_table('listings',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('external_id', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('image_url', sa.String(), nullable=True),
        sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(), nullable=False),
        sa.Column('shipping_cost', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('condition', sa.String(), nullable=False),
        sa.Column('seller', sa.String(), nullable=False),
        sa.Column('seller_location', sa.String(), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'external_id', name='uq_listing_source_ext_id')
    )
    op.create_index(op.f('ix_listings_source'), 'listings', ['source'], unique=False)
    op.create_index(op.f('ix_listings_is_active'), 'listings', ['is_active'], unique=False)

    # listing_snapshots table
    op.create_table('listing_snapshots',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('listing_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(), nullable=False),
        sa.Column('shipping_cost', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('condition', sa.String(), nullable=False),
        sa.Column('seller', sa.String(), nullable=False),
        sa.Column('seller_location', sa.String(), nullable=False),
        sa.Column('image_url', sa.String(), nullable=True),
        sa.Column('captured_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['listing_id'], ['listings.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_listing_snapshots_listing_id'), 'listing_snapshots', ['listing_id'], unique=False)

    # part_matches table
    op.create_table('part_matches',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('listing_id', sa.Integer(), nullable=False),
        sa.Column('part_id', sa.String(), nullable=False),
        sa.Column('part_name', sa.String(), nullable=False),
        sa.Column('total_score', sa.Integer(), nullable=False),
        sa.Column('reasons_json', sa.Text(), nullable=False),
        sa.Column('algorithm_version', sa.String(), nullable=False),
        sa.Column('matched_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['listing_id'], ['listings.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_part_matches_listing_id'), 'part_matches', ['listing_id'], unique=False)
    op.create_index(op.f('ix_part_matches_part_id'), 'part_matches', ['part_id'], unique=False)

    # notifications table
    op.create_table('notifications',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('listing_id', sa.Integer(), nullable=False),
        sa.Column('match_id', sa.Integer(), nullable=True),
        sa.Column('alert_type', sa.String(), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['listing_id'], ['listings.id'], ),
        sa.ForeignKeyConstraint(['match_id'], ['part_matches.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notifications_listing_id'), 'notifications', ['listing_id'], unique=False)

    # search_runs table
    op.create_table('search_runs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('total_found', sa.Integer(), nullable=False),
        sa.Column('new_listings', sa.Integer(), nullable=False),
        sa.Column('updated_listings', sa.Integer(), nullable=False),
        sa.Column('matches_found', sa.Integer(), nullable=False),
        sa.Column('alerts_sent', sa.Integer(), nullable=False),
        sa.Column('errors_json', sa.Text(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('search_runs')
    op.drop_index(op.f('ix_notifications_listing_id'), table_name='notifications')
    op.drop_table('notifications')
    op.drop_index(op.f('ix_part_matches_part_id'), table_name='part_matches')
    op.drop_index(op.f('ix_part_matches_listing_id'), table_name='part_matches')
    op.drop_table('part_matches')
    op.drop_index(op.f('ix_listing_snapshots_listing_id'), table_name='listing_snapshots')
    op.drop_table('listing_snapshots')
    op.drop_index(op.f('ix_listings_is_active'), table_name='listings')
    op.drop_index(op.f('ix_listings_source'), table_name='listings')
    op.drop_table('listings')
