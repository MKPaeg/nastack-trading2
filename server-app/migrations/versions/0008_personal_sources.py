"""Keep current tenant catalog as defaults, copy independent personal lists."""

from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_source_lists",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "personal_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("template_id", sa.String(36), sa.ForeignKey("source_catalog.id"), nullable=True),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "domain"),
    )
    op.create_index("ix_personal_sources_tenant_id", "personal_sources", ["tenant_id"])
    op.create_index("ix_personal_sources_owner_id", "personal_sources", ["owner_id"])
    db = op.get_bind()
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=db)
    defaults = sa.Table("source_catalog", metadata, autoload_with=db)
    lists = sa.Table("user_source_lists", metadata, autoload_with=db)
    personal = sa.Table("personal_sources", metadata, autoload_with=db)
    for user in db.execute(sa.select(users.c.id, users.c.tenant_id)).mappings():
        db.execute(lists.insert().values(owner_id=user["id"], initialized_at=datetime.now(timezone.utc)))
        for row in db.execute(
            sa.select(defaults).where(defaults.c.tenant_id == user["tenant_id"])
        ).mappings():
            db.execute(
                personal.insert().values(
                    id=str(uuid4()),
                    tenant_id=user["tenant_id"],
                    owner_id=user["id"],
                    template_id=row["id"],
                    domain=row["domain"],
                    version=row["version"],
                    value=row["value"],
                    updated_at=row["updated_at"],
                )
            )
    # Existing audit events and analysis snapshots remain untouched.


def downgrade():
    raise RuntimeError("Personal source edits would be lost. Restore a backup instead of downgrading.")
