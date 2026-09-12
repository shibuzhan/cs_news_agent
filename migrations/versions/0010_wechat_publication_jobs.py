"""add wechat publication jobs

Revision ID: 0010_wechat_publication_jobs
Revises: 0009_manual_publication
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_wechat_publication_jobs"
down_revision = "0009_manual_publication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wechat_publication_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("cover_attachment_id", sa.String(length=36), nullable=False),
        sa.Column("cover_media_id", sa.String(length=500), nullable=True),
        sa.Column("inline_attachment_ids_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inline_image_urls_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("wechat_draft_media_id", sa.String(length=500), nullable=True),
        sa.Column("wechat_publish_id", sa.String(length=500), nullable=True),
        sa.Column("published_url", sa.String(length=2048), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("submit_idempotency_key", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cover_attachment_id"], ["chat_attachments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id"),
        sa.UniqueConstraint("wechat_draft_media_id"),
        sa.UniqueConstraint("wechat_publish_id"),
        sa.UniqueConstraint("submit_idempotency_key"),
    )
    op.create_index("ix_wechat_publication_jobs_draft_id", "wechat_publication_jobs", ["draft_id"])
    op.create_index("ix_wechat_publication_jobs_state", "wechat_publication_jobs", ["state"])
    op.create_index("ix_wechat_publication_jobs_cover_attachment_id", "wechat_publication_jobs", ["cover_attachment_id"])


def downgrade() -> None:
    op.drop_index("ix_wechat_publication_jobs_cover_attachment_id", table_name="wechat_publication_jobs")
    op.drop_index("ix_wechat_publication_jobs_state", table_name="wechat_publication_jobs")
    op.drop_index("ix_wechat_publication_jobs_draft_id", table_name="wechat_publication_jobs")
    op.drop_table("wechat_publication_jobs")
