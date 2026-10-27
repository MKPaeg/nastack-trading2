"""Owner-scoped trading policies and idempotent order intents."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trade_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("broker", sa.String(10), nullable=False),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "broker", "code"),
    )
    op.create_index("ix_trade_plans_owner_id", "trade_plans", ["owner_id"])
    op.create_table(
        "trade_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("trade_plans.id"), nullable=False),
        sa.Column("request_key", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "request_key"),
    )
    op.create_index("ix_trade_orders_owner_id", "trade_orders", ["owner_id"])


def downgrade():
    raise RuntimeError("Back up trading records before destructive downgrade.")
