"""track current attempt start for reused generation records

Revision ID: 0022_run_attempt_started
Revises: 0021_draft_source_snapshots
"""

from alembic import op
import sqlalchemy as sa


revision = "0022_run_attempt_started"
down_revision = "0021_draft_source_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_agent_runs",
        sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 旧记录的首次创建时间就是其唯一一次尝试的开始时间。
    op.execute(
        "UPDATE chat_agent_runs "
        "SET attempt_started_at = created_at "
        "WHERE attempt_started_at IS NULL"
    )
    op.alter_column("chat_agent_runs", "attempt_started_at", nullable=False)
    op.create_index(
        "ix_chat_agent_runs_attempt_started_at",
        "chat_agent_runs",
        ["attempt_started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_agent_runs_attempt_started_at", table_name="chat_agent_runs")
    op.drop_column("chat_agent_runs", "attempt_started_at")
