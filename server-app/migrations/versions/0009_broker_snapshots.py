"""Read-only broker market and portfolio snapshots, scoped to one user."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "broker_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(6), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_broker_snapshots_tenant_id", "broker_snapshots", ["tenant_id"])
    op.create_index("ix_broker_snapshots_owner_id", "broker_snapshots", ["owner_id"])
    op.create_index(
        "ix_broker_snapshots_lookup", "broker_snapshots", ["owner_id", "kind", "symbol", "created_at"]
    )


def downgrade():
    raise RuntimeError("Broker snapshots must be backed up before a destructive downgrade.")
