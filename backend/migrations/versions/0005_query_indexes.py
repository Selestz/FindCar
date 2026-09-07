"""Indexes for scheduled work, latest runs and per-listing event history."""

import sqlalchemy as sa
from alembic import op

revision = "0005_query_indexes"
down_revision = "0004_monitoring"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_jobs_running_source_lease",
        "refresh_jobs",
        ["source", "lease_until"],
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index("ix_runs_search_requested", "search_runs", ["search_id", sa.text("requested_at DESC")])
    op.create_index(
        "ix_events_listing_type_date",
        "events",
        ["user_id", "listing_id", "type", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_search_listings_detail_queue",
        "search_listings",
        ["search_id", "last_detail_attempt_at", "listing_id"],
    )


def downgrade():
    op.drop_index("ix_search_listings_detail_queue", table_name="search_listings")
    op.drop_index("ix_events_listing_type_date", table_name="events")
    op.drop_index("ix_runs_search_requested", table_name="search_runs")
    op.drop_index("ix_jobs_running_source_lease", table_name="refresh_jobs")
