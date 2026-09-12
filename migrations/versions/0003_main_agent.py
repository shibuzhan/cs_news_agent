"""增加受控主 Agent 的父级执行审计。

Revision ID: 0003_main_agent
Revises: 0002_collection_reliability
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_main_agent"
down_revision = "0002_collection_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("requested_sources_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limit", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("tool_results_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.add_column(
        "collection_runs",
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_collection_runs_agent_run_id_agent_runs",
        "collection_runs",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_collection_runs_agent_run_id", "collection_runs", ["agent_run_id"])


def downgrade() -> None:
    op.drop_index("ix_collection_runs_agent_run_id", table_name="collection_runs")
    op.drop_constraint(
        "fk_collection_runs_agent_run_id_agent_runs",
        "collection_runs",
        type_="foreignkey",
    )
    op.drop_column("collection_runs", "agent_run_id")
    op.drop_table("agent_runs")
