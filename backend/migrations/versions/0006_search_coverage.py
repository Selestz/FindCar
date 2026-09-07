"""Persist progressive search position and actual detail verification times."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_search_coverage"
down_revision = "0005_query_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("listings", sa.Column("detail_checked_at", sa.DateTime(timezone=True)))
    op.add_column("listings", sa.Column("detail_attempted_at", sa.DateTime(timezone=True)))
    # Old catalog timestamps are not evidence of a successful detail check.
    op.add_column(
        "saved_search_source_states",
        sa.Column("scan_next_page", sa.Integer(), nullable=False, server_default="2"),
    )
    op.add_column(
        "saved_search_source_states",
        sa.Column("scan_filters_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("refresh_jobs", sa.Column("pages_checked", JSONB(), nullable=False, server_default="[]"))
    op.add_column(
        "refresh_jobs", sa.Column("catalog_complete", sa.Boolean(), nullable=False, server_default="false")
    )


def downgrade():
    op.drop_column("refresh_jobs", "catalog_complete")
    op.drop_column("refresh_jobs", "pages_checked")
    op.drop_column("saved_search_source_states", "scan_filters_version")
    op.drop_column("saved_search_source_states", "scan_next_page")
    op.drop_column("listings", "detail_attempted_at")
    op.drop_column("listings", "detail_checked_at")
