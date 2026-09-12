"""保存增强生成的规划、引用和质检审计信息。

Revision ID: 0007_enhanced_generation
Revises: 0006_draft_evidence
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_enhanced_generation"
down_revision = "0006_draft_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = (
        ("content_plan_json", "'{}'::jsonb"),
        ("quality_report_json", "'{}'::jsonb"),
        ("claim_citations_json", "'[]'::jsonb"),
    )
    for name, default in columns:
        op.add_column("content_drafts", sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text(default)))
        op.alter_column("content_drafts", name, server_default=None)
    op.add_column("content_drafts", sa.Column("generation_mode", sa.String(length=40), nullable=False, server_default="baseline"))
    op.alter_column("content_drafts", "generation_mode", server_default=None)


def downgrade() -> None:
    for name in ("generation_mode", "claim_citations_json", "quality_report_json", "content_plan_json"):
        op.drop_column("content_drafts", name)
