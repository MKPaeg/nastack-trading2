"""Explicit unlimited administrator sessions and 168-hour ordinary-user ceiling."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("auth_sessions") as batch:
        batch.alter_column("expires_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    connection = op.get_bind()
    # Never resurrect an expired, revoked, restricted or non-administrator session.
    connection.execute(
        sa.text("""UPDATE auth_sessions SET expires_at=NULL
        WHERE revoked_at IS NULL AND expires_at>CURRENT_TIMESTAMP
        AND user_id IN (SELECT id FROM users WHERE role='admin' AND active=true)""")
    )
    connection.execute(
        sa.text("UPDATE admin_settings SET max_session_hours=168, version=version+1 WHERE id=1")
    )


def downgrade():
    op.get_bind().execute(
        sa.text("UPDATE auth_sessions SET expires_at=CURRENT_TIMESTAMP WHERE expires_at IS NULL")
    )
    with op.batch_alter_table("auth_sessions") as batch:
        batch.alter_column("expires_at", existing_type=sa.DateTime(timezone=True), nullable=False)
