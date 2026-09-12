"""allow regenerated drafts after discard

Revision ID: 0019_reusable_source_drafts
Revises: 0018_notifications
Create Date: 2026-09-09
"""

from alembic import op


revision = "0019_reusable_source_drafts"
down_revision = "0018_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL 为 0001 中未命名的唯一约束生成此约束名。
    op.drop_constraint("content_drafts_source_item_id_key", "content_drafts", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        "content_drafts_source_item_id_key", "content_drafts", ["source_item_id"]
    )
