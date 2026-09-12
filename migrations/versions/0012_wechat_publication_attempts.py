"""allow multiple WeChat publication attempts for one content draft

Revision ID: 0012_wechat_publication_attempts
Revises: 0011_publication_assets
Create Date: 2026-09-09
"""

from alembic import op


revision = "0012_wechat_publication_attempts"
down_revision = "0011_publication_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史任务保留，只移除“一篇文案只能有一条投递任务”的限制。"""
    op.execute(
        "ALTER TABLE wechat_publication_jobs "
        "DROP CONSTRAINT IF EXISTS wechat_publication_jobs_draft_id_key"
    )


def downgrade() -> None:
    """降级前拒绝存在多次投递，避免静默丢失审计记录。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM wechat_publication_jobs
                GROUP BY draft_id HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION 'cannot restore unique draft_id while multiple WeChat attempts exist';
            END IF;
        END $$;
        """
    )
    op.create_unique_constraint(
        "uq_wechat_publication_jobs_draft_id",
        "wechat_publication_jobs",
        ["draft_id"],
    )
