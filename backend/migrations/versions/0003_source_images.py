"""Persist allowlisted source image URLs for thumbnails and matching."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_source_images"
down_revision = "00af18fb3260"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("main_image_url", sa.Text(), nullable=True))
    op.add_column(
        "listings", sa.Column("images", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("listings", "images")
    op.drop_column("listings", "main_image_url")
