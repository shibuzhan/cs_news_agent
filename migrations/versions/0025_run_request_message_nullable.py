"""allow agent-triggered runs without a user request message

Revision ID: 0025_run_request_message_nullable
Revises: 0024_image_job_style
"""

from alembic import op
import sqlalchemy as sa


revision = "0025_run_request_message"
down_revision = "0024_image_job_style"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Agent 工具（投递、配图、审核）发起的运行没有对应的用户消息：
    # 之前该列 NOT NULL，工具建运行会直接 IntegrityError（NotNullViolation）。
    op.alter_column(
        "chat_agent_runs",
        "request_message_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "chat_agent_runs",
        "request_message_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
