"""Durable owner-scoped cumulative sell P/L ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "realized_sales",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("trade_orders.id"), nullable=False),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("trade_plans.id"), nullable=False),
        sa.Column("broker", sa.String(10), nullable=False),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("trade_day", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("proceeds", sa.Numeric(24, 6), nullable=False),
        sa.Column("cost", sa.Numeric(24, 6), nullable=True),
        sa.Column("profit", sa.Numeric(24, 6), nullable=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index("ix_realized_owner_day", "realized_sales", ["owner_id", "tenant_id", "trade_day"])


def downgrade():
    op.drop_table("realized_sales")
