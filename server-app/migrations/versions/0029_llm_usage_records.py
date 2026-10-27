"""Persist owner-scoped per-call LLM usage and historical estimated cost."""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "llm_usage_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("local_day", sa.Date(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cached_input_tokens", sa.Integer()),
        sa.Column("cache_write_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("estimated_usd", sa.Numeric(18, 10)),
        sa.Column("pricing_checked_on", sa.Date()),
    )
    op.create_index("ix_llm_usage_owner_day", "llm_usage_records", ["owner_id", "tenant_id", "local_day"])


def downgrade():
    op.drop_index("ix_llm_usage_owner_day", table_name="llm_usage_records")
    op.drop_table("llm_usage_records")
