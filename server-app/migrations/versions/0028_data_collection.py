"""Owner-scoped collection policies, attempts, budgets and restart-safe scheduling."""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("data_collection_policies", "data_collection_tasks"):
        columns = [
            sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("value", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        ]
        if name.endswith("policies"):
            columns.append(sa.Column("version", sa.Integer(), nullable=False))
        else:
            columns.extend(
                [
                    sa.Column("resource", sa.String(100), primary_key=True),
                    sa.Column("status", sa.String(20), nullable=False),
                    sa.Column("next_at", sa.DateTime(timezone=True), nullable=False),
                ]
            )
        op.create_table(name, *columns)
        op.create_index(f"ix_{name}_tenant_id", name, ["tenant_id"])


def downgrade():
    for name in ("data_collection_tasks", "data_collection_policies"):
        op.drop_index(f"ix_{name}_tenant_id", table_name=name)
        op.drop_table(name)
