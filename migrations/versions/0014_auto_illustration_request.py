"""add auto illustration request flag

Revision ID: 0014_auto_illustration_request
Revises: 0013_generated_images_reviews
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_auto_illustration_request"
down_revision = "0013_generated_images_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_agent_runs",
        sa.Column("auto_illustration_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("chat_agent_runs", "auto_illustration_requested", server_default=None)


def downgrade() -> None:
    op.drop_column("chat_agent_runs", "auto_illustration_requested")
