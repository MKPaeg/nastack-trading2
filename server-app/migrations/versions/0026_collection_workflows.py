"""Owner-scoped incremental collection workflow state."""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collection_workflows",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("code", sa.String(6), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_collection_workflows_tenant_id", "collection_workflows", ["tenant_id"])


def downgrade():
    op.drop_index("ix_collection_workflows_tenant_id", table_name="collection_workflows")
    op.drop_table("collection_workflows")
