"""Tenant-shared source metadata and immutable run snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_catalog",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "domain"),
    )
    op.create_index("ix_source_catalog_tenant_id", "source_catalog", ["tenant_id"])
    op.add_column(
        "analysis_runs", sa.Column("source_catalog_snapshot", sa.JSON(), nullable=False, server_default="[]")
    )


def downgrade():
    op.drop_column("analysis_runs", "source_catalog_snapshot")
    op.drop_table("source_catalog")
