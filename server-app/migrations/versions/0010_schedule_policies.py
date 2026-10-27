"""Persist per-user recurring-task cadence plans without activating automation."""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


DEFAULT_PLAN = {
    "holdings_minutes": 5,
    "holdings_market_hours_only": True,
    "analysis_minutes": 60,
    "news_minutes": 15,
    "instrument_master_minutes": 1440,
}


def upgrade():
    op.create_table(
        "schedule_policies",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_schedule_policies_tenant_id", "schedule_policies", ["tenant_id"])
    db = op.get_bind()
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=db)
    policies = sa.Table("schedule_policies", metadata, autoload_with=db)
    for user in db.execute(sa.select(users.c.id, users.c.tenant_id)).mappings():
        db.execute(policies.insert().values(
            owner_id=user["id"], tenant_id=user["tenant_id"], version=1,
            value=dict(DEFAULT_PLAN), updated_at=datetime.now(timezone.utc),
        ))


def downgrade():
    raise RuntimeError("Back up schedule policies before a destructive downgrade.")
