"""为聚合草稿保存完整证据链。

Revision ID: 0006_draft_evidence
Revises: 0005_conversation_agent
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_draft_evidence"
down_revision = "0005_conversation_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content_drafts",
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("content_drafts", "evidence_json", server_default=None)


def downgrade() -> None:
    op.drop_column("content_drafts", "evidence_json")
