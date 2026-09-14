"""配图/重写未完成时的审核排队：避免“审核早于它要审的内容”。

真实故障（2026-09-13 16:29）：用户要求“生成插图和封面图，然后审核”，
配图是后台任务、审核紧接着入队 → 审核在正文插图完成前 8 秒就开始了，
而且随后的汇报还说“自动审核未被触发”。

真实故障（2026-09-14 09:53）：正文重写（9–12 分钟）在跑，用户紧接着说“再跑一轮审核”，
审核立刻开始 → 审的是马上会被替换掉的旧版本，随后两个写者抢同一个草稿版本号，
后完成的那次以唯一约束冲突失败，用户看到“生成失败”而正文其实已经改了。

做法：`run_auto_review` 发现该草稿仍有在跑/排队的配图任务**或在跑的重写**时不立即入队，
只登记一个待审标记；等它们进入终态后由收束逻辑（`process_collection_finalizer`、
`process_draft_regeneration_job`）读取该标记并真正发起审核。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.storage.repositories import ContentRepository

logger = logging.getLogger(__name__)

KEY_PREFIX = "pending.auto_review."
ACTIVE_IMAGE_STATUSES = ("queued", "running")


def pending_key(draft_id: str) -> str:
    return f"{KEY_PREFIX}{draft_id}"


def mark_pending_review(
    repository: ContentRepository, draft_id: str, *, deliver: bool, chat_run_id: str | None,
    revise: bool = True,
) -> None:
    """登记“前面的任务结束后自动审核”。

    `revise` 必须一起记下来：用户点的是“只审不改”还是“审核并改稿”，排队不能把口径弄丢。
    """
    repository.set_app_setting(
        pending_key(draft_id),
        json.dumps({"deliver": bool(deliver), "chat_run_id": chat_run_id or "", "revise": bool(revise)}),
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


def read_pending_review(repository: ContentRepository, draft_id: str) -> dict[str, Any] | None:
    """读取待审标记但**不清除**：给只读的队列查询用。"""
    raw = repository.get_app_setting(pending_key(draft_id))
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def merge_pending_review(
    repository: ContentRepository, draft_id: str, *, deliver: bool, revise: bool,
    chat_run_id: str | None = None,
) -> dict[str, Any]:
    """把新的诉求并进**已经在跑的那次审核**（不新建审核、不丢用户的投递意图）。

    真实场景：用户先说“仅运行审核”，紧接着说“审核如果通过就投递草稿箱”。第二次请求
    带着 deliver=True 撞上正在跑的审核——旧实现只回一句“已在处理中”，投递意图被丢掉，
    审核通过后也不会投递。这里把 deliver / revise 合并保存，由审核任务结束时读取执行。
    """
    current = read_pending_review(repository, draft_id) or {}
    merged = {
        "deliver": bool(current.get("deliver")) or bool(deliver),
        "revise": bool(current.get("revise", True)) or bool(revise),
        "chat_run_id": str(current.get("chat_run_id") or chat_run_id or ""),
        "merged_at": datetime.now(UTC).isoformat(),
    }
    repository.set_app_setting(
        pending_key(draft_id), json.dumps(merged, ensure_ascii=False), updated_by="agent"
    )
    logger.info(
        "pending_review_merged draft_id=%s deliver=%s revise=%s", draft_id, merged["deliver"], merged["revise"]
    )
    return merged


def pending_review_drafts(repository: ContentRepository, draft_ids: list[str]) -> list[str]:
    return [draft_id for draft_id in draft_ids if repository.get_app_setting(pending_key(draft_id))]


async def launch_pending_reviews(
    settings: Settings, repository: ContentRepository, draft_ids: list[str]
) -> list[str]:
    """前置任务结束后发起此前排队的审核，返回真正入队的草稿 id 列表。"""
    from app.jobs import enqueue_auto_review_job

    launched: list[str] = []
    for draft_id in draft_ids:
        pending = pop_pending_review(repository, draft_id)
        if pending is None:
            continue
        deliver = bool(pending.get("deliver"))
        revise = bool(pending.get("revise", True))
        chat_run_id = str(pending.get("chat_run_id") or "") or None
        review_run = repository.create_auto_review_run(draft_id, None, status="queued")
        repository.expire_stale_auto_review_run(draft_id, settings.collection_job_timeout_seconds)
        job_id = await enqueue_auto_review_job(
            settings, draft_id, review_run.id, deliver, chat_run_id, revise
        )
        if chat_run_id:
            repository.add_chat_agent_event(
                chat_run_id, "前置任务已完成，开始审核",
                f"排队的审核已自动开始（{'改稿一轮' if revise else '只出意见、不改稿'}）；"
                f"审核任务编号：{job_id}。",
                "running",
                metadata={
                    "phase": "review", "state": "running", "job_id": job_id,
                    "draft_ids": [draft_id], "revise": revise,
                },
            )
        launched.append(draft_id)
        logger.info(
            "pending_review_launched draft_id=%s deliver=%s revise=%s job_id=%s",
            draft_id, deliver, revise, job_id,
        )
    return launched
