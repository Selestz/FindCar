"""Persistent scheduling, source budgets and known listing checks."""

import sqlalchemy as sa
from alembic import op

revision = "0004_monitoring"
down_revision = "0003_source_images"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("saved_searches", sa.Column("next_refresh_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_searches_due", "saved_searches", ["next_refresh_at"], postgresql_where=sa.text("enabled IS true")
    )
    op.add_column("search_runs", sa.Column("trigger", sa.String(20), nullable=False, server_default="manual"))
    op.add_column("search_listings", sa.Column("last_detail_attempt_at", sa.DateTime(timezone=True)))
    op.create_table(
        "source_limits",
        sa.Column("source", sa.String(20), primary_key=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("next_request_at", sa.DateTime(timezone=True)),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer, nullable=False, server_default="0"),
    )
    # Stagger existing searches instead of fetching all of them on deployment.
    op.execute(
        "UPDATE saved_searches SET next_refresh_at = now() + (refresh_interval_seconds + floor(random()*121)) * interval '1 second' WHERE enabled"
    )


def downgrade():
    op.drop_table("source_limits")
    op.drop_column("search_listings", "last_detail_attempt_at")
    op.drop_column("search_runs", "trigger")
    op.drop_index("ix_searches_due", table_name="saved_searches")
    op.drop_column("saved_searches", "next_refresh_at")
