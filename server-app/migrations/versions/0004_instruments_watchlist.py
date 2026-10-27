"""Domestic instruments and owner-isolated watchlists; preserve historical jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instruments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "code"),
    )
    op.create_index("ix_instruments_tenant_id", "instruments", ["tenant_id"])
    op.create_table(
        "watchlist_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("instrument_id", sa.String(36), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "instrument_id"),
    )
    op.create_index("ix_watchlist_items_tenant_id", "watchlist_items", ["tenant_id"])
    op.create_index("ix_watchlist_items_owner_id", "watchlist_items", ["owner_id"])
    op.add_column(
        "analysis_runs", sa.Column("instrument_snapshot", sa.JSON(), nullable=False, server_default="{}")
    )


def downgrade():
    op.drop_column("analysis_runs", "instrument_snapshot")
    op.drop_table("watchlist_items")
    op.drop_table("instruments")
