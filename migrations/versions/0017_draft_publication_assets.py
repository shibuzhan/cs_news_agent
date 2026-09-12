"""bind publication assets to individual drafts

Revision ID: 0017_draft_publication_assets
Revises: 0016_image_generation_jobs
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_draft_publication_assets"
down_revision = "0016_image_generation_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "draft_publication_assets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("draft_id", sa.String(length=36), sa.ForeignKey("content_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", sa.String(length=36), sa.ForeignKey("publication_assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("draft_id", "asset_id", name="uq_draft_publication_asset"),
    )
    op.create_index("ix_draft_publication_assets_draft_id", "draft_publication_assets", ["draft_id"])
    op.create_index("ix_draft_publication_assets_asset_id", "draft_publication_assets", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_draft_publication_assets_asset_id", table_name="draft_publication_assets")
    op.drop_index("ix_draft_publication_assets_draft_id", table_name="draft_publication_assets")
    op.drop_table("draft_publication_assets")
