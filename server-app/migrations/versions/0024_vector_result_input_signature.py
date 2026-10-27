"""Index personal vector cache lookups by their immutable input signature."""

import sqlalchemy as sa
from alembic import op


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vector_results", sa.Column("input_signature", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_vector_result_input_signature",
        "vector_results",
        ["tenant_id", "owner_id", "input_signature"],
    )


def downgrade():
    op.drop_index("ix_vector_result_input_signature", table_name="vector_results")
    op.drop_column("vector_results", "input_signature")
