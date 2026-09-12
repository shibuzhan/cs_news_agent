"""record manual publication before GitHub introduction deduplication

Revision ID: 0009_manual_publication
Revises: 0008_project_introductions
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_manual_publication"
down_revision = "0008_project_introductions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_drafts", sa.Column("published_platform", sa.String(length=100), nullable=True))
    op.add_column("content_drafts", sa.Column("published_url", sa.String(length=2048), nullable=True))
    op.add_column("content_drafts", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "publication_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("operator", sa.String(length=100), nullable=False),
        sa.Column("platform", sa.String(length=100), nullable=False),
        sa.Column("published_url", sa.String(length=2048), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_publication_records_draft_id", "publication_records", ["draft_id"])
    op.add_column(
        "project_introductions",
        sa.Column("publication_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_project_introductions_publication_id",
        "project_introductions",
        "publication_records",
        ["publication_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_project_introductions_publication_id",
        "project_introductions",
        ["publication_id"],
    )
    op.create_index(
        "ix_project_introductions_publication_id",
        "project_introductions",
        ["publication_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_introductions_publication_id", table_name="project_introductions")
    op.drop_constraint("uq_project_introductions_publication_id", "project_introductions", type_="unique")
    op.drop_constraint("fk_project_introductions_publication_id", "project_introductions", type_="foreignkey")
    op.drop_column("project_introductions", "publication_id")
    op.drop_index("ix_publication_records_draft_id", table_name="publication_records")
    op.drop_table("publication_records")
    op.drop_column("content_drafts", "published_at")
    op.drop_column("content_drafts", "published_url")
    op.drop_column("content_drafts", "published_platform")
