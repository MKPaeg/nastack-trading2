"""Historical mock schema: retained for databases already upgraded to 0020.

Broker mock runtime and UI have been removed. These dormant tables are kept
only to preserve migration continuity and existing encrypted records/history.
No application routes or workers access them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "mock_brokers",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("broker", sa.String(10), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("secrets", sa.Text(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
    )
    op.create_index("ix_mock_brokers_tenant_id", "mock_brokers", ["tenant_id"])
    op.create_table(
        "mock_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("broker", sa.String(10), nullable=False),
        sa.Column("request_key", sa.String(150), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "request_key"),
    )
    op.create_index("ix_mock_orders_owner_id", "mock_orders", ["owner_id"])


def downgrade():
    raise RuntimeError("Back up mock credentials and journal before downgrade.")
