"""创建第一版资讯、草稿和审核表。

Revision ID: 0001_initial
Revises:
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "source_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("external_id", sa.String(length=500), nullable=False),
        sa.Column("source_name", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("author", sa.String(length=500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("duplicate_of_id", sa.String(length=36), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("category_confidence", sa.Float(), nullable=False),
        sa.Column("hot_score", sa.Float(), nullable=False),
        sa.Column("metrics_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["duplicate_of_id"], ["source_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_kind", "external_id", name="uq_source_external_id"),
    )
    op.create_index("ix_source_items_source_kind", "source_items", ["source_kind"])
    op.create_index("ix_source_items_content_hash", "source_items", ["content_hash"])
    op.create_index("ix_source_items_duplicate_of_id", "source_items", ["duplicate_of_id"])
    op.create_index("ix_source_items_category", "source_items", ["category"])
    op.create_index("ix_source_items_hot_score", "source_items", ["hot_score"])

    op.create_table(
        "content_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_item_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("title_options_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary_cn", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tags_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("card_script_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_name", sa.String(length=200), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_item_id"),
    )
    op.create_index("ix_content_drafts_source_item_id", "content_drafts", ["source_item_id"])
    op.create_index("ix_content_drafts_status", "content_drafts", ["status"])

    op.create_table(
        "review_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("reviewer", sa.String(length=100), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_review_events_draft_id", "review_events", ["draft_id"])


def downgrade() -> None:
    op.drop_table("review_events")
    op.drop_table("content_drafts")
    op.drop_table("source_items")
