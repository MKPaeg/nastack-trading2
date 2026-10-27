"""Owner-scoped vector selections/results and daily news provenance."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    def owner():
        return sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False)

    def tenant():
        return sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False)

    def created():
        return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)

    op.create_table(
        "vector_selections",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        tenant(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "vector_results",
        sa.Column("id", sa.String(36), primary_key=True),
        owner(),
        tenant(),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        created(),
        sa.UniqueConstraint("owner_id", "fingerprint"),
    )
    op.create_index("ix_vector_results_owner_id", "vector_results", ["owner_id"])
    op.create_table(
        "news_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        owner(),
        tenant(),
        sa.Column("news_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        created(),
    )
    op.create_index("ix_news_runs_owner_id", "news_runs", ["owner_id"])
    op.create_index("ix_news_runs_news_date", "news_runs", ["news_date"])
    op.create_table(
        "news_articles",
        sa.Column("id", sa.String(36), primary_key=True),
        owner(),
        tenant(),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("personal_sources.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("news_date", sa.Date(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        created(),
        sa.UniqueConstraint("owner_id", "fingerprint"),
    )
    op.create_index("ix_news_articles_owner_id", "news_articles", ["owner_id"])
    op.create_index("ix_news_articles_news_date", "news_articles", ["news_date"])


def downgrade():
    raise RuntimeError("Back up vector and news history before destructive downgrade.")
