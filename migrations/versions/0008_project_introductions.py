"""record introduced GitHub projects

Revision ID: 0008_project_introductions
Revises: 0007_enhanced_generation
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_project_introductions"
down_revision = "0007_enhanced_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_introductions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("external_id", sa.String(length=500), nullable=False),
        sa.Column("source_item_id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("introduced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_kind", "external_id", name="uq_project_introduction_source"),
        sa.UniqueConstraint("source_item_id"),
        sa.UniqueConstraint("draft_id"),
    )
    op.create_index("ix_project_introductions_source_kind", "project_introductions", ["source_kind"])
    op.create_index("ix_project_introductions_source_item_id", "project_introductions", ["source_item_id"])
    op.create_index("ix_project_introductions_draft_id", "project_introductions", ["draft_id"])


def downgrade() -> None:
    op.drop_index("ix_project_introductions_draft_id", table_name="project_introductions")
    op.drop_index("ix_project_introductions_source_item_id", table_name="project_introductions")
    op.drop_index("ix_project_introductions_source_kind", table_name="project_introductions")
    op.drop_table("project_introductions")
