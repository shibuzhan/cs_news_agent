"""add persistent failure notifications

Revision ID: 0018_notifications
Revises: 0017_draft_publication_assets
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_notifications"
down_revision = "0017_draft_publication_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("target_view", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_type", "source_id", name="uq_notification_source"),
    )
    op.create_index("ix_notifications_source_type", "notifications", ["source_type"])
    op.create_index("ix_notifications_source_id", "notifications", ["source_id"])
    op.create_index("ix_notifications_category", "notifications", ["category"])
    op.create_index("ix_notifications_is_read", "notifications", ["is_read"])
    op.create_index("ix_notifications_dismissed_at", "notifications", ["dismissed_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_dismissed_at", table_name="notifications")
    op.drop_index("ix_notifications_is_read", table_name="notifications")
    op.drop_index("ix_notifications_category", table_name="notifications")
    op.drop_index("ix_notifications_source_id", table_name="notifications")
    op.drop_index("ix_notifications_source_type", table_name="notifications")
    op.drop_table("notifications")
