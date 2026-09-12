"""增加采集运行记录和 GitHub Trending 快照。

Revision ID: 0002_collection_reliability
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_collection_reliability"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collection_runs_source_kind", "collection_runs", ["source_kind"])
    op.create_index("ix_collection_runs_status", "collection_runs", ["status"])

    op.create_table(
        "github_trending_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collection_run_id", sa.String(length=36), nullable=False),
        sa.Column("source_item_id", sa.String(length=36), nullable=False),
        sa.Column("period", sa.String(length=20), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("stars_period", sa.Integer(), nullable=False),
        sa.Column("stars_total", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["collection_run_id"], ["collection_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collection_run_id",
            "source_item_id",
            "period",
            name="uq_trending_snapshot_run_item_period",
        ),
    )
    op.create_index(
        "ix_github_trending_snapshots_collection_run_id",
        "github_trending_snapshots",
        ["collection_run_id"],
    )
    op.create_index(
        "ix_github_trending_snapshots_source_item_id",
        "github_trending_snapshots",
        ["source_item_id"],
    )
    op.create_index(
        "ix_github_trending_snapshots_period",
        "github_trending_snapshots",
        ["period"],
    )


def downgrade() -> None:
    op.drop_table("github_trending_snapshots")
    op.drop_table("collection_runs")
