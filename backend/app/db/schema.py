import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def now() -> datetime:
    return datetime.now(UTC)


def pk() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def uid(name: str, target: str, *, nullable: bool = False, delete: str = "CASCADE") -> sa.Column:
    return sa.Column(
        name, UUID(as_uuid=True), sa.ForeignKey(target, ondelete=delete), nullable=nullable, index=True
    )


def stamp(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, default=None if nullable else now)


users = sa.Table(
    "users",
    metadata,
    pk(),
    sa.Column("username", sa.String(80), nullable=False),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("role", sa.String(10), nullable=False, server_default="user"),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    stamp("created_at"),
    sa.CheckConstraint("role IN ('user','admin')", name="user_role"),
)
sa.Index("uq_users_username_lower", sa.func.lower(users.c.username), unique=True)
sessions = sa.Table(
    "sessions",
    metadata,
    pk(),
    uid("user_id", "users.id"),
    sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
    sa.Column("csrf_token", sa.String(80), nullable=False),
    stamp("created_at"),
    stamp("expires_at"),
)
login_limits = sa.Table(
    "login_limits",
    metadata,
    sa.Column("key", sa.String(64), primary_key=True),
    sa.Column("attempts", sa.Integer, nullable=False),
    stamp("window_start"),
)
searches = sa.Table(
    "saved_searches",
    metadata,
    pk(),
    uid("user_id", "users.id"),
    sa.Column("name", sa.String(150), nullable=False),
    sa.Column("filters", JSONB, nullable=False),
    sa.Column("filters_version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("enabled_sources", JSONB, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("refresh_interval_seconds", sa.Integer, nullable=False, server_default="1500"),
    stamp("created_at"),
    stamp("last_checked_at", nullable=True),
    stamp("last_manual_refresh_at", nullable=True),
    stamp("next_refresh_at", nullable=True),
    sa.UniqueConstraint("id", "user_id"),
    sa.CheckConstraint("refresh_interval_seconds >= 1200", name="search_interval"),
)
sa.Index("ix_searches_due", searches.c.next_refresh_at, postgresql_where=searches.c.enabled.is_(True))
runs = sa.Table(
    "search_runs",
    metadata,
    pk(),
    uid("search_id", "saved_searches.id"),
    uid("user_id", "users.id"),
    sa.Column("filters", JSONB, nullable=False),
    sa.Column("filters_version", sa.Integer, nullable=False),
    sa.Column("outcome", sa.String(20), nullable=False, server_default="pending"),
    sa.Column("trigger", sa.String(20), nullable=False, server_default="manual"),
    stamp("requested_at"),
    stamp("completed_at", nullable=True),
    sa.ForeignKeyConstraint(
        ["search_id", "user_id"], ["saved_searches.id", "saved_searches.user_id"], ondelete="CASCADE"
    ),
)
jobs = sa.Table(
    "refresh_jobs",
    metadata,
    pk(),
    uid("run_id", "search_runs.id"),
    uid("search_id", "saved_searches.id"),
    sa.Column("source", sa.String(20), nullable=False),
    sa.Column("state", sa.String(20), nullable=False, server_default="queued"),
    sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
    sa.Column("lease_token", UUID(as_uuid=True)),
    stamp("lease_until", nullable=True),
    stamp("not_before"),
    stamp("completed_at", nullable=True),
    sa.Column("error_code", sa.String(80)),
    sa.Column("result_count", sa.Integer),
    sa.Column("warnings", JSONB, nullable=False, server_default="[]"),
    sa.UniqueConstraint("run_id", "source"),
    sa.CheckConstraint("state IN ('queued','running','complete','failed')", name="job_state"),
)
sa.Index(
    "uq_jobs_active",
    jobs.c.search_id,
    jobs.c.source,
    unique=True,
    postgresql_where=jobs.c.state.in_(["queued", "running"]),
)
sa.Index("ix_jobs_ready", jobs.c.not_before, postgresql_where=jobs.c.state == "queued")
sa.Index(
    "ix_jobs_running_source_lease",
    jobs.c.source,
    jobs.c.lease_until,
    postgresql_where=jobs.c.state == "running",
)
sa.Index("ix_runs_search_requested", runs.c.search_id, runs.c.requested_at.desc())
source_states = sa.Table(
    "saved_search_source_states",
    metadata,
    sa.Column(
        "search_id",
        UUID(as_uuid=True),
        sa.ForeignKey("saved_searches.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("source", sa.String(20), primary_key=True),
    sa.Column("status", sa.String(30), nullable=False),
    stamp("last_attempt_at", nullable=True),
    stamp("last_success_at", nullable=True),
    sa.Column("last_error", sa.String(80)),
    sa.Column("last_result_count", sa.Integer),
)
health = sa.Table(
    "source_health",
    metadata,
    sa.Column("source", sa.String(20), primary_key=True),
    sa.Column("status", sa.String(30), nullable=False),
    stamp("last_attempt_at", nullable=True),
    stamp("last_success_at", nullable=True),
    sa.Column("last_error", sa.String(80)),
)
source_limits = sa.Table(
    "source_limits",
    metadata,
    sa.Column("source", sa.String(20), primary_key=True),
    stamp("cooldown_until", nullable=True),
    stamp("next_request_at", nullable=True),
    stamp("window_started_at"),
    sa.Column("request_count", sa.Integer, nullable=False, server_default="0"),
)
heartbeats = sa.Table(
    "worker_heartbeats",
    metadata,
    sa.Column("worker_id", sa.String(80), primary_key=True),
    stamp("last_seen_at"),
)
listings = sa.Table(
    "listings",
    metadata,
    pk(),
    sa.Column("source", sa.String(20), nullable=False),
    sa.Column("source_listing_id", sa.String(128), nullable=False),
    sa.Column("source_url", sa.Text, nullable=False),
    sa.Column("title", sa.String(500), nullable=False),
    *[
        sa.Column(n, sa.String(100))
        for n in (
            "make",
            "model",
            "generation",
            "body_type",
            "engine_type",
            "transmission",
            "drive_type",
            "steering_wheel",
            "color",
            "region",
            "city",
        )
    ],
    *[sa.Column(n, sa.Integer) for n in ("year", "power_hp", "mileage_km", "owners_count")],
    sa.Column("engine_volume", sa.Numeric(6, 3)),
    sa.Column("price", sa.Numeric(14, 2)),
    sa.Column("currency", sa.String(3), nullable=False),
    sa.Column("seller_type", sa.String(20), nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("main_image_url", sa.Text),
    sa.Column("images", JSONB, nullable=False, server_default="[]"),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("source_metadata", JSONB, nullable=False, server_default="{}"),
    sa.Column("field_observed_at", JSONB, nullable=False, server_default="{}"),
    sa.Column("parser_version", sa.String(40), nullable=False),
    sa.Column("normalization_version", sa.String(40), nullable=False),
    stamp("published_at", nullable=True),
    stamp("first_seen_at"),
    stamp("last_seen_at"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.UniqueConstraint("source", "source_listing_id"),
    sa.CheckConstraint("price IS NULL OR price >= 0", name="listing_price"),
    sa.CheckConstraint("mileage_km IS NULL OR mileage_km >= 0", name="listing_mileage"),
    sa.CheckConstraint("status IN ('ACTIVE','REMOVED','UNKNOWN')", name="listing_status"),
)
sa.Index("ix_listings_candidates", listings.c.make, listings.c.model, listings.c.year, listings.c.mileage_km)
search_listings = sa.Table(
    "search_listings",
    metadata,
    sa.Column(
        "search_id",
        UUID(as_uuid=True),
        sa.ForeignKey("saved_searches.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("listing_id", UUID(as_uuid=True), sa.ForeignKey("listings.id"), primary_key=True, index=True),
    uid("user_id", "users.id"),
    stamp("first_seen_at"),
    stamp("last_seen_at"),
    sa.Column("match_state", sa.String(20), nullable=False),
    sa.Column("unknown_filters", JSONB, nullable=False),
    sa.Column("filters_version", sa.Integer, nullable=False),
    stamp("last_detail_attempt_at", nullable=True),
    sa.ForeignKeyConstraint(
        ["search_id", "user_id"], ["saved_searches.id", "saved_searches.user_id"], ondelete="CASCADE"
    ),
)
clusters = sa.Table(
    "vehicle_clusters",
    metadata,
    pk(),
    uid("user_id", "users.id"),
    uid("representative_listing_id", "listings.id", delete="RESTRICT"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    stamp("first_seen_at"),
    stamp("last_seen_at"),
    stamp("archived_at", nullable=True),
    sa.Column("favourite", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("hidden", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("notes", JSONB, nullable=False, server_default="[]"),
    sa.UniqueConstraint("id", "user_id"),
)
memberships = sa.Table(
    "cluster_memberships",
    metadata,
    sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("listing_id", UUID(as_uuid=True), sa.ForeignKey("listings.id"), primary_key=True, index=True),
    sa.Column("cluster_id", UUID(as_uuid=True), nullable=False, index=True),
    stamp("attached_at"),
    sa.ForeignKeyConstraint(
        ["cluster_id", "user_id"], ["vehicle_clusters.id", "vehicle_clusters.user_id"], ondelete="CASCADE"
    ),
)
snapshots = sa.Table(
    "listing_snapshots",
    metadata,
    pk(),
    uid("listing_id", "listings.id", delete="RESTRICT"),
    sa.Column("listing_version", sa.Integer, nullable=False),
    stamp("observed_at"),
    sa.Column("price", sa.Numeric(14, 2)),
    sa.Column("currency", sa.String(3), nullable=False),
    sa.Column("mileage_km", sa.Integer),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("description_hash", sa.String(64)),
    sa.Column("description_text_on_change", sa.Text),
    sa.UniqueConstraint("listing_id", "listing_version"),
)
events = sa.Table(
    "events",
    metadata,
    pk(),
    uid("user_id", "users.id"),
    uid("search_id", "saved_searches.id", nullable=True, delete="SET NULL"),
    uid("listing_id", "listings.id", nullable=True, delete="RESTRICT"),
    uid("snapshot_id", "listing_snapshots.id", nullable=True, delete="RESTRICT"),
    sa.Column("type", sa.String(40), nullable=False),
    sa.Column("event_key", sa.String(200), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    stamp("occurred_at"),
    stamp("read_at", nullable=True),
    sa.UniqueConstraint("user_id", "event_key"),
)
sa.Index("ix_events_user_date", events.c.user_id, events.c.occurred_at.desc(), events.c.id)
sa.Index(
    "ix_events_listing_type_date",
    events.c.user_id,
    events.c.listing_id,
    events.c.type,
    events.c.occurred_at.desc(),
)
sa.Index(
    "ix_search_listings_detail_queue",
    search_listings.c.search_id,
    search_listings.c.last_detail_attempt_at,
    search_listings.c.listing_id,
)

fingerprints = sa.Table(
    "listing_fingerprints",
    metadata,
    sa.Column(
        "listing_id", UUID(as_uuid=True), sa.ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("photos", JSONB, nullable=False),
    stamp("observed_at"),
)


def pair_columns() -> list:
    return [
        uid("user_id", "users.id"),
        uid("left_id", "listings.id"),
        uid("right_id", "listings.id"),
        sa.CheckConstraint("left_id < right_id", name="canonical_pair"),
        *[
            sa.ForeignKeyConstraint(
                ["user_id", side],
                ["cluster_memberships.user_id", "cluster_memberships.listing_id"],
                ondelete="CASCADE",
            )
            for side in ("left_id", "right_id")
        ],
    ]


candidates = sa.Table(
    "duplicate_candidates",
    metadata,
    pk(),
    *pair_columns(),
    sa.Column("decision", sa.String(30), nullable=False),
    sa.Column("evidence", JSONB, nullable=False),
    stamp("updated_at"),
    sa.UniqueConstraint("user_id", "left_id", "right_id"),
)
decisions = sa.Table(
    "duplicate_decisions",
    metadata,
    pk(),
    *pair_columns(),
    sa.Column("decision", sa.String(10), nullable=False),
    stamp("created_at"),
    stamp("superseded_at", nullable=True),
    sa.CheckConstraint("decision IN ('same','different')", name="manual_decision"),
)
sa.Index(
    "uq_decisions_active",
    decisions.c.user_id,
    decisions.c.left_id,
    decisions.c.right_id,
    unique=True,
    postgresql_where=decisions.c.superseded_at.is_(None),
)
operations = sa.Table(
    "cluster_operations",
    metadata,
    pk(),
    uid("user_id", "users.id"),
    sa.Column("kind", sa.String(30), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    stamp("created_at"),
)
relist_links = sa.Table(
    "relist_links",
    metadata,
    pk(),
    uid("user_id", "users.id"),
    uid("old_id", "listings.id"),
    uid("new_id", "listings.id"),
    sa.Column("evidence", JSONB, nullable=False),
    stamp("created_at"),
    sa.UniqueConstraint("user_id", "old_id", "new_id"),
    *[
        sa.ForeignKeyConstraint(
            ["user_id", side],
            ["cluster_memberships.user_id", "cluster_memberships.listing_id"],
            ondelete="CASCADE",
        )
        for side in ("old_id", "new_id")
    ],
)
