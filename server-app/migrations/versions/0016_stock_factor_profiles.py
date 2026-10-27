"""Owner-scoped empirical stock attributes for evidence-grounded news analysis."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stock_factor_profiles",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("code", sa.String(6), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_stock_factor_profiles_tenant_id", "stock_factor_profiles", ["tenant_id"])


def downgrade():
    op.drop_table("stock_factor_profiles")
