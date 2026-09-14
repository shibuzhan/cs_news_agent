"""记录生成长任务（重写）的目标草稿与已入队任务号，用于入队前去重

Revision ID: 0028_run_target_draft
Revises: 0027_runtime_settings_center
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0028_run_target_draft"
down_revision = "0027_runtime_settings_center"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 为什么需要这一列：同一条用户消息会**两条路径各入队一次**重写——模型直接调
    # `rewrite_draft` 工具一次，意图兜底（`_step_rewrite`）再一次。两个任务并发写同一草稿，
    # 后完成的那次撞上 uq_draft_revision_version（真实故障：用户报“聊天框异常”，
    # `process_draft_regeneration_job` 以 IntegrityError 失败，草稿版本却已经涨过）。
    # 这一列记下“本次运行正在写哪篇草稿”，配合 0029 的 rewrite_job_id 才能判断是否已入队。
    op.add_column(
        "chat_agent_runs",
        sa.Column("target_draft_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_chat_agent_runs_target_draft_id",
        "chat_agent_runs",
        ["target_draft_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_agent_runs_target_draft_id", table_name="chat_agent_runs")
    op.drop_column("chat_agent_runs", "target_draft_id")
