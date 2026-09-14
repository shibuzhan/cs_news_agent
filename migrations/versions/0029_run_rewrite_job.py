"""记录已入队的重写任务号（去重判断用）

Revision ID: 0029_run_rewrite_job
Revises: 0028_run_target_draft
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0029_run_rewrite_job"
down_revision = "0028_run_target_draft"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 同一条用户消息会两条路径各调一次重写（模型工具 + 意图兜底），两次用的是**同一个运行**：
    # target_draft_id 只能说明“这次运行在写哪篇”，看不出“是否已经入队过”，于是任务被入队两次、
    # 并发写同一草稿并以草稿版本唯一冲突收场（真实故障：聊天框里两个“正在处理中”）。
    # 这一列记下已经入队的任务号，第二次请求据此直接复用。
    op.add_column(
        "chat_agent_runs",
        sa.Column("rewrite_job_id", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_agent_runs", "rewrite_job_id")
