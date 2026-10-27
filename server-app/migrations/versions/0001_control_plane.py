"""Initial user/settings/job/audit schema. Keep this revision immutable."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def pk(name="id"):
    return sa.Column(name, sa.String(36), primary_key=True)


def tenant():
    return sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False)


def owner(name="owner_id"):
    return sa.Column(name, sa.String(36), sa.ForeignKey("users.id"), nullable=False)


def stamp(name="created_at", nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade():
    op.create_table(
        "tenants",
        pk(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
    )
    op.create_table(
        "users",
        pk(),
        tenant(),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("code_hash", sa.String(256), nullable=False),
        stamp(),
        sa.UniqueConstraint("tenant_id", "email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        owner("user_id"),
        stamp("expires_at"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table(
        "login_throttles",
        sa.Column("bucket", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        stamp("window_at"),
    )
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        tenant(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        stamp("updated_at"),
    )
    op.create_index("ix_user_settings_tenant_id", "user_settings", ["tenant_id"])
    op.create_table(
        "analysis_runs",
        pk(),
        tenant(),
        owner(),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(6), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(200), nullable=False),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("settings_snapshot", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("claim_token", sa.String(36), nullable=True),
        stamp("lease_until", True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        stamp(),
        stamp("started_at", True),
        stamp("finished_at", True),
        sa.UniqueConstraint("owner_id", "request_key"),
    )
    for name in ("tenant_id", "owner_id"):
        op.create_index(f"ix_analysis_runs_{name}", "analysis_runs", [name])
    op.create_index("ix_runs_queue", "analysis_runs", ["status", "created_at"])
    op.create_table(
        "audit_events",
        pk(),
        tenant(),
        owner("actor_id"),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        stamp(),
    )
    for name in ("tenant_id", "actor_id"):
        op.create_index(f"ix_audit_events_{name}", "audit_events", [name])
    op.create_table(
        "system_logs",
        pk(),
        tenant(),
        owner(),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("message", sa.String(200), nullable=False),
        stamp(),
    )
    for name in ("tenant_id", "owner_id", "created_at"):
        op.create_index(f"ix_system_logs_{name}", "system_logs", [name])


def downgrade():
    for table in (
        "system_logs",
        "audit_events",
        "analysis_runs",
        "user_settings",
        "login_throttles",
        "auth_sessions",
        "users",
        "tenants",
    ):
        op.drop_table(table)
