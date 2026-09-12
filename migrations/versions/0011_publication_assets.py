"""add independent publication assets

Revision ID: 0011_publication_assets
Revises: 0010_wechat_publication_jobs
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_publication_assets"
down_revision = "0010_wechat_publication_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_publication_assets_sha256", "publication_assets", ["sha256"])
    op.alter_column(
        "wechat_publication_jobs",
        "cover_attachment_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.add_column(
        "wechat_publication_jobs",
        sa.Column("cover_asset_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "wechat_publication_jobs",
        sa.Column(
            "inline_asset_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_foreign_key(
        "fk_wechat_publication_jobs_cover_asset_id",
        "wechat_publication_jobs",
        "publication_assets",
        ["cover_asset_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_wechat_publication_jobs_cover_asset_id",
        "wechat_publication_jobs",
        ["cover_asset_id"],
    )
    op.alter_column(
        "wechat_publication_jobs",
        "inline_asset_ids_json",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_index("ix_wechat_publication_jobs_cover_asset_id", table_name="wechat_publication_jobs")
    op.drop_constraint(
        "fk_wechat_publication_jobs_cover_asset_id",
        "wechat_publication_jobs",
        type_="foreignkey",
    )
    op.drop_column("wechat_publication_jobs", "inline_asset_ids_json")
    op.drop_column("wechat_publication_jobs", "cover_asset_id")
    op.alter_column(
        "wechat_publication_jobs",
        "cover_attachment_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.drop_index("ix_publication_assets_sha256", table_name="publication_assets")
    op.drop_table("publication_assets")
