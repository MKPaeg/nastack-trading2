"""Administrator-assigned session duration, including existing users."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("session_hours", sa.Integer(), nullable=False, server_default="24"))
    settings = op.create_table(
        "admin_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("default_session_hours", sa.Integer(), nullable=False),
        sa.Column("max_session_hours", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("code_ttl_minutes", sa.Integer(), nullable=False),
        sa.Column("code_max_attempts", sa.Integer(), nullable=False),
    )
    op.bulk_insert(
        settings,
        [
            {
                "id": 1,
                "default_session_hours": 24,
                "max_session_hours": 72,
                "version": 1,
                "code_ttl_minutes": 10,
                "code_max_attempts": 5,
            }
        ],
    )
    op.create_table(
        "login_challenges",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code_hash", sa.String(256), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_login_challenges_user_id", "login_challenges", ["user_id"])
    op.create_table(
        "remembered_logins",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_remembered_logins_user_id", "remembered_logins", ["user_id"])


def downgrade():
    op.drop_table("login_challenges")
    op.drop_table("remembered_logins")
    op.drop_table("admin_settings")
    op.drop_column("users", "session_hours")
