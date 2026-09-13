"""app_settings：Agent 的长期运行偏好。

Revision ID: 0026_app_settings
Revises: 0025_run_request_message
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_app_settings"
down_revision = "0025_run_request_message"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=80), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(length=60), nullable=False, server_default="agent"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
