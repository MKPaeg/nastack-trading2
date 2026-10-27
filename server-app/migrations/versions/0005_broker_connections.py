"""Owner-isolated read-only Namuh authentication policy and encrypted token cache."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "broker_connections",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=True),
        sa.Column("token_scope", sa.String(64), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_broker_connections_tenant_id", "broker_connections", ["tenant_id"])
    op.create_table(
        "broker_probes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("reused_token", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_broker_probes_owner_id", "broker_probes", ["owner_id"])
    op.create_index("ix_broker_probes_tenant_id", "broker_probes", ["tenant_id"])


def downgrade():
    op.drop_table("broker_probes")
    op.drop_table("broker_connections")
