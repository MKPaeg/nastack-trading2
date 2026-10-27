"""User-owned paper wallets and append-only simulation journal."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "paper_wallets",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
    )
    op.create_index("ix_paper_wallets_tenant_id", "paper_wallets", ["tenant_id"])
    op.create_table(
        "paper_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_paper_events_owner_id", "paper_events", ["owner_id"])


def downgrade():
    raise RuntimeError("Paper journal must be backed up before downgrade.")
