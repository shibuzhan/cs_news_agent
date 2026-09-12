"""增加通用对话运行摘要和待确认计划。

Revision ID: 0005_conversation_agent
Revises: 0004_chat_attachments
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_conversation_agent"
down_revision = "0004_chat_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_agent_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("request_message_id", sa.String(length=36), nullable=False),
        sa.Column("response_message_id", sa.String(length=36), nullable=True),
        sa.Column("intent", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("tool_results_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["request_message_id"], ["chat_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["response_message_id"], ["chat_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, column in (
        ("ix_chat_agent_runs_session_id", "session_id"),
        ("ix_chat_agent_runs_request_message_id", "request_message_id"),
        ("ix_chat_agent_runs_response_message_id", "response_message_id"),
        ("ix_chat_agent_runs_intent", "intent"),
        ("ix_chat_agent_runs_status", "status"),
    ):
        op.create_index(name, "chat_agent_runs", [column])
    op.create_table(
        "chat_agent_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["chat_agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_chat_agent_event_sequence"),
    )
    op.create_index("ix_chat_agent_events_run_id", "chat_agent_events", ["run_id"])
    op.create_table(
        "schedule_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("chat_agent_run_id", sa.String(length=36), nullable=False),
        sa.Column("schedule_text", sa.String(length=500), nullable=False),
        sa.Column("task_summary", sa.Text(), nullable=False),
        sa.Column("sources_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("confirmation_key", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chat_agent_run_id"], ["chat_agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("confirmation_key"),
    )
    op.create_index("ix_schedule_plans_session_id", "schedule_plans", ["session_id"])
    op.create_index("ix_schedule_plans_chat_agent_run_id", "schedule_plans", ["chat_agent_run_id"])
    op.create_index("ix_schedule_plans_status", "schedule_plans", ["status"])
    op.create_table(
        "publish_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("chat_agent_run_id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=True),
        sa.Column("platform", sa.String(length=100), nullable=False),
        sa.Column("request_summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("confirmation_key", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chat_agent_run_id"], ["chat_agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("confirmation_key"),
    )
    op.create_index("ix_publish_plans_session_id", "publish_plans", ["session_id"])
    op.create_index("ix_publish_plans_chat_agent_run_id", "publish_plans", ["chat_agent_run_id"])
    op.create_index("ix_publish_plans_draft_id", "publish_plans", ["draft_id"])
    op.create_index("ix_publish_plans_status", "publish_plans", ["status"])


def downgrade() -> None:
    op.drop_table("publish_plans")
    op.drop_table("schedule_plans")
    op.drop_table("chat_agent_events")
    op.drop_table("chat_agent_runs")
