"""add structured chat session memory

Revision ID: 0020_chat_session_memory
Revises: 0019_reusable_source_drafts
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_chat_session_memory"
down_revision = "0019_reusable_source_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_session_memories",
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("active_draft_id", sa.String(length=36), sa.ForeignKey("content_drafts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("active_attachment_id", sa.String(length=36), sa.ForeignKey("chat_attachments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_session_memories_active_draft_id", "chat_session_memories", ["active_draft_id"])
    op.create_index("ix_chat_session_memories_active_attachment_id", "chat_session_memories", ["active_attachment_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_session_memories_active_attachment_id", table_name="chat_session_memories")
    op.drop_index("ix_chat_session_memories_active_draft_id", table_name="chat_session_memories")
    op.drop_table("chat_session_memories")
