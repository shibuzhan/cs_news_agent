"""store the planned style for each image job

Revision ID: 0024_image_job_style
Revises: 0023_image_job_subject
"""

from alembic import op
import sqlalchemy as sa


revision = "0024_image_job_style"
down_revision = "0023_image_job_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可空：历史任务没有规划风格，出图时按草稿 ID 在来源风格池内轮换。
    op.add_column(
        "image_generation_jobs",
        sa.Column("style", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("image_generation_jobs", "style")
