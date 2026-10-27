"""Preserve ended sessions and browser metadata; legacy creation time stays unknown."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("auth_sessions") as batch:
        batch.add_column(sa.Column("public_id", sa.String(36), nullable=True))
        for name in ["created_at", "last_seen_at", "metadata_recorded_at", "revoked_at"]:
            batch.add_column(sa.Column(name, sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("end_reason", sa.String(40), nullable=True))
        for name, size, default in [
            ("device", 40, "기존 세션"),
            ("browser", 40, "정보 없음"),
            ("user_agent", 500, ""),
            ("client_ip", 45, ""),
        ]:
            batch.add_column(sa.Column(name, sa.String(size), nullable=False, server_default=default))
    table = sa.table("auth_sessions", sa.column("token_hash"), sa.column("public_id"))
    connection = op.get_bind()
    for token_hash in connection.execute(sa.select(table.c.token_hash)).scalars().all():
        connection.execute(
            table.update().where(table.c.token_hash == token_hash).values(public_id=str(uuid4()))
        )
    with op.batch_alter_table("auth_sessions") as batch:
        batch.alter_column("public_id", existing_type=sa.String(36), nullable=False)
        batch.create_unique_constraint("uq_auth_sessions_public_id", ["public_id"])


def downgrade():
    with op.batch_alter_table("auth_sessions") as batch:
        batch.drop_constraint("uq_auth_sessions_public_id", type_="unique")
        for name in [
            "public_id",
            "created_at",
            "last_seen_at",
            "metadata_recorded_at",
            "revoked_at",
            "end_reason",
            "device",
            "browser",
            "user_agent",
            "client_ip",
        ]:
            batch.drop_column(name)
