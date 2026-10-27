"""Personal web-grounded business description history."""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "business_research",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("request_key", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "request_key"),
    )
    op.create_index(
        "ix_business_research_owner_code",
        "business_research",
        ["tenant_id", "owner_id", "code", "created_at"],
    )


def downgrade():
    op.drop_index("ix_business_research_owner_code", table_name="business_research")
    op.drop_table("business_research")
