"""模型档案与系统设置变更审计。

Revision ID: 0027_runtime_settings_center
Revises: 0026_app_settings
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0027_runtime_settings_center"
down_revision = "0026_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_model_profile_name"),
    )
    op.create_table(
        "runtime_setting_audits",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("scope", sa.String(length=80), nullable=False),
        sa.Column("setting_key", sa.String(length=160), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.String(length=60), nullable=False, server_default="运营人员"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_runtime_setting_audits_scope", "runtime_setting_audits", ["scope"])
    op.create_index("ix_runtime_setting_audits_setting_key", "runtime_setting_audits", ["setting_key"])


def downgrade() -> None:
    op.drop_index("ix_runtime_setting_audits_setting_key", table_name="runtime_setting_audits")
    op.drop_index("ix_runtime_setting_audits_scope", table_name="runtime_setting_audits")
    op.drop_table("runtime_setting_audits")
    op.drop_table("model_profiles")
