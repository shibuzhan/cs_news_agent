"""add independent image generation jobs

Revision ID: 0016_image_generation_jobs
Revises: 0015_draft_revision_history
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_image_generation_jobs"
down_revision = "0015_draft_revision_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_generation_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("chat_agent_run_id", sa.String(length=36), sa.ForeignKey("chat_agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("draft_id", sa.String(length=36), sa.ForeignKey("content_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False, server_default="inline"),
        sa.Column("placement_after_paragraph", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("arq_job_id", sa.String(length=100), nullable=True, unique=True),
        sa.Column("illustration_id", sa.String(length=36), sa.ForeignKey("draft_illustrations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_image_generation_jobs_chat_agent_run_id", "image_generation_jobs", ["chat_agent_run_id"])
    op.create_index("ix_image_generation_jobs_draft_id", "image_generation_jobs", ["draft_id"])
    op.create_index("ix_image_generation_jobs_status", "image_generation_jobs", ["status"])
    op.create_index("ix_image_generation_jobs_illustration_id", "image_generation_jobs", ["illustration_id"])


def downgrade() -> None:
    op.drop_index("ix_image_generation_jobs_illustration_id", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_status", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_draft_id", table_name="image_generation_jobs")
    op.drop_index("ix_image_generation_jobs_chat_agent_run_id", table_name="image_generation_jobs")
    op.drop_table("image_generation_jobs")
