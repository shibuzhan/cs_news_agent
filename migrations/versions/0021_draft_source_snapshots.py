"""store private source snapshots for draft regeneration

Revision ID: 0021_draft_source_snapshots
Revises: 0020_chat_session_memory
"""

from alembic import op
import sqlalchemy as sa


revision = "0021_draft_source_snapshots"
down_revision = "0020_chat_session_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "draft_source_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("source_item_id", sa.String(length=36), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("content_origin", sa.String(length=80), nullable=False, server_default="github_readme"),
        sa.Column("object_key", sa.String(length=500), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_length", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", name="uq_draft_source_snapshot_draft"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_draft_source_snapshots_draft_id", "draft_source_snapshots", ["draft_id"])
    op.create_index("ix_draft_source_snapshots_source_item_id", "draft_source_snapshots", ["source_item_id"])
    op.create_index("ix_draft_source_snapshots_sha256", "draft_source_snapshots", ["sha256"])


def downgrade() -> None:
    op.drop_index("ix_draft_source_snapshots_sha256", table_name="draft_source_snapshots")
    op.drop_index("ix_draft_source_snapshots_source_item_id", table_name="draft_source_snapshots")
    op.drop_index("ix_draft_source_snapshots_draft_id", table_name="draft_source_snapshots")
    op.drop_table("draft_source_snapshots")
