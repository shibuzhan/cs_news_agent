"""add generated illustration and auto review audit

Revision ID: 0013_generated_images_reviews
Revises: 0012_wechat_publication_attempts
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013_generated_images_reviews"
down_revision = "0012_wechat_publication_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_agent_runs",
        sa.Column("auto_review_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("chat_agent_runs", "auto_review_requested", server_default=None)
    op.create_table(
        "draft_illustrations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False, server_default="inline"),
        sa.Column("placement_after_paragraph", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["publication_assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_draft_illustrations_draft_id", "draft_illustrations", ["draft_id"])
    op.create_index("ix_draft_illustrations_asset_id", "draft_illustrations", ["asset_id"])
    op.create_table(
        "auto_review_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("chat_agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("rule_report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("model_report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("wechat_job_id", sa.String(length=36), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chat_agent_run_id"], ["chat_agent_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auto_review_runs_draft_id", "auto_review_runs", ["draft_id"])
    op.create_index("ix_auto_review_runs_chat_agent_run_id", "auto_review_runs", ["chat_agent_run_id"])
    op.create_index("ix_auto_review_runs_status", "auto_review_runs", ["status"])


def downgrade() -> None:
    op.drop_table("auto_review_runs")
    op.drop_table("draft_illustrations")
    op.drop_column("chat_agent_runs", "auto_review_requested")
