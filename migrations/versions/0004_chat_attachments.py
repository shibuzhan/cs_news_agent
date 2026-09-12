"""增加对话、私有文本附件和附件处理审计。

Revision ID: 0004_chat_attachments
Revises: 0003_main_agent
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_chat_attachments"
down_revision = "0003_main_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])
    op.create_table(
        "chat_attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["chat_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_chat_attachments_session_id", "chat_attachments", ["session_id"])
    op.create_index("ix_chat_attachments_message_id", "chat_attachments", ["message_id"])
    op.create_index("ix_chat_attachments_sha256", "chat_attachments", ["sha256"])
    op.create_index("ix_chat_attachments_status", "chat_attachments", ["status"])
    op.create_table(
        "attachment_processing_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attachment_id", sa.String(length=36), nullable=False),
        sa.Column("request_message_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["attachment_id"], ["chat_attachments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_message_id"], ["chat_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attachment_processing_runs_attachment_id",
        "attachment_processing_runs",
        ["attachment_id"],
    )
    op.create_index(
        "ix_attachment_processing_runs_request_message_id",
        "attachment_processing_runs",
        ["request_message_id"],
    )
    op.create_index(
        "ix_attachment_processing_runs_status",
        "attachment_processing_runs",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("attachment_processing_runs")
    op.drop_table("chat_attachments")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
