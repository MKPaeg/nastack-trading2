"""Administrator-only persistent deployment-wide trading circuit breakers."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "admin_settings",
        sa.Column("server_orders_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "admin_settings",
        sa.Column("server_automatic_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "admin_settings",
        sa.Column("trading_safety_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("admin_settings", "trading_safety_version")
    op.drop_column("admin_settings", "server_automatic_locked")
    op.drop_column("admin_settings", "server_orders_locked")
