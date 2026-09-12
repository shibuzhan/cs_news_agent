"""add automatic draft revision history

Revision ID: 0015_draft_revision_history
Revises: 0014_auto_illustration_request
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015_draft_revision_history"
down_revision = "0014_auto_illustration_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "draft_revisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("draft_id", sa.String(length=36), sa.ForeignKey("content_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("auto_review_run_id", sa.String(length=36), sa.ForeignKey("auto_review_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("summary_cn", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tags_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("revision_reason_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("draft_id", "version", name="uq_draft_revision_version"),
    )
    op.create_index("ix_draft_revisions_draft_id", "draft_revisions", ["draft_id"])
    op.create_index("ix_draft_revisions_auto_review_run_id", "draft_revisions", ["auto_review_run_id"])


def downgrade() -> None:
    op.drop_index("ix_draft_revisions_auto_review_run_id", table_name="draft_revisions")
    op.drop_index("ix_draft_revisions_draft_id", table_name="draft_revisions")
    op.drop_table("draft_revisions")
