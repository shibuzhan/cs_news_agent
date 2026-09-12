"""store the planned subject for each image job

Revision ID: 0023_image_job_subject
Revises: 0022_run_attempt_started
"""

from alembic import op
import sqlalchemy as sa


revision = "0023_image_job_subject"
down_revision = "0022_run_attempt_started"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可空：历史任务没有规划主体，出图时按草稿 ID 在来源实物池内轮换。
    op.add_column(
        "image_generation_jobs",
        sa.Column("subject", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("image_generation_jobs", "subject")
