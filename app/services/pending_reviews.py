"""配图未完成时的审核排队：避免“审核早于配图”。

真实故障（2026-09-13 16:29）：用户要求“生成插图和封面图，然后审核”，
配图是后台任务、审核紧接着入队 → 审核在正文插图完成前 8 秒就开始了，
而且随后的汇报还说“自动审核未被触发”。

做法：`run_auto_review` 发现该草稿仍有排队/运行中的配图任务时**不立即入队**，
只登记一个待审标记；配图全部进入终态后由收束逻辑（`process_collection_finalizer`）
读取该标记并真正发起审核。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings
from app.storage.repositories import ContentRepository

logger = logging.getLogger(__name__)

KEY_PREFIX = "pending.auto_review."
ACTIVE_IMAGE_STATUSES = ("queued", "running")


def pending_key(draft_id: str) -> str:
    return f"{KEY_PREFIX}{draft_id}"


def mark_pending_review(
    repository: ContentRepository, draft_id: str, *, deliver: bool, chat_run_id: str | None
) -> None:
    """登记“配图完成后自动审核”。"""
    repository.set_app_setting(
        pending_key(draft_id),
        json.dumps({"deliver": bool(deliver), "chat_run_id": chat_run_id or ""}),
        updated_by="agent",
    )


def pop_pending_review(repository: ContentRepository, draft_id: str) -> dict[str, Any] | None:
    """取出并清除待审标记；没有标记返回 None。"""
    raw = repository.get_app_setting(pending_key(draft_id))
    if not raw:
        return None
    repository.set_app_setting(pending_key(draft_id), "", updated_by="delivery")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def pending_review_drafts(repository: ContentRepository, draft_ids: list[str]) -> list[str]:
    return [draft_id for draft_id in draft_ids if repository.get_app_setting(pending_key(draft_id))]


async def launch_pending_reviews(
    settings: Settings, repository: ContentRepository, draft_ids: list[str]
) -> list[str]:
    """配图终态后发起此前排队的审核，返回真正入队的草稿 id 列表。"""
    from app.jobs import enqueue_auto_review_job

    launched: list[str] = []
    for draft_id in draft_ids:
        pending = pop_pending_review(repository, draft_id)
        if pending is None:
            continue
        deliver = bool(pending.get("deliver"))
        chat_run_id = str(pending.get("chat_run_id") or "") or None
        review_run = repository.create_auto_review_run(draft_id, None, status="queued")
        repository.expire_stale_auto_review_run(draft_id, settings.collection_job_timeout_seconds)
        job_id = await enqueue_auto_review_job(settings, draft_id, review_run.id, deliver, chat_run_id)
        if chat_run_id:
            repository.add_chat_agent_event(
                chat_run_id, "配图已完成，开始审核",
                f"配图任务已全部结束；审核任务编号：{job_id}。",
                "running",
                metadata={"phase": "review", "state": "running", "job_id": job_id, "draft_ids": [draft_id]},
            )
        launched.append(draft_id)
        logger.info(
            "pending_review_launched draft_id=%s deliver=%s job_id=%s", draft_id, deliver, job_id
        )
    return launched
