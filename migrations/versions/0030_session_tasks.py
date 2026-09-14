"""会话任务清单：跨消息存活、带依赖、可被界面与回执渲染

Revision ID: 0030_session_tasks
Revises: 0029_run_rewrite_job
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0030_session_tasks"
down_revision = "0029_run_rewrite_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 为什么要有这张表：用户反馈“agent 收到什么消息就立刻去执行”，缺的正是**跨消息的任务清单**。
    # 有了它，伙伴关系（“投递”依赖“审核通过”）与进度都能落库，回执与生成记录可以照着渲染。
    op.create_table(
        "session_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False, server_default="chat"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column(
            "depends_on_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("draft_id", sa.String(length=36), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_session_tasks_session_id", "session_tasks", ["session_id"])
    op.create_index("ix_session_tasks_position", "session_tasks", ["position"])
    op.create_index("ix_session_tasks_kind", "session_tasks", ["kind"])
    op.create_index("ix_session_tasks_status", "session_tasks", ["status"])
    op.create_index("ix_session_tasks_draft_id", "session_tasks", ["draft_id"])


def downgrade() -> None:
    op.drop_index("ix_session_tasks_draft_id", table_name="session_tasks")
    op.drop_index("ix_session_tasks_status", table_name="session_tasks")
    op.drop_index("ix_session_tasks_kind", table_name="session_tasks")
    op.drop_index("ix_session_tasks_position", table_name="session_tasks")
    op.drop_index("ix_session_tasks_session_id", table_name="session_tasks")
    op.drop_table("session_tasks")
