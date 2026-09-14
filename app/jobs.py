from __future__ import annotations

import logging
from typing import Any

from arq import create_pool
from arq.connections import RedisSettings

from app.config import Settings


logger = logging.getLogger("news_agent.jobs")


def redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def enqueue_collection_job(
    settings: Settings,
    run_id: str,
    session_id: str,
    response_message_id: str,
    sources: list[str],
    limit: int,
    auto_review_requested: bool = False,
    auto_illustration_requested: bool = False,
    target: str | None = None,
) -> str:
    logger.info(
        "collection_enqueue_started run_id=%s sources=%s limit=%s target=%s",
        run_id,
        sources,
        limit,
        target or "-",
    )
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job(
            "process_collection_job",
            run_id,
            session_id,
            response_message_id,
            sources,
            limit,
            auto_review_requested,
            auto_illustration_requested,
            target,
        )
        if job is None:
            raise RuntimeError("后台任务未能入队")
        logger.info("collection_enqueue_finished run_id=%s job_id=%s", run_id, job.job_id)
        return job.job_id
    finally:
        await pool.aclose()


async def enqueue_draft_regeneration_job(
    settings: Settings,
    run_id: str,
    session_id: str,
    response_message_id: str,
    draft_id: str,
    auto_review_requested: bool = False,
    auto_illustration_requested: bool = False,
) -> str:
    """在既有生成记录内重生成同一草稿，绝不创建第二条聊天运行记录。"""
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job(
            "process_draft_regeneration_job", run_id, session_id, response_message_id,
            draft_id, auto_review_requested, auto_illustration_requested,
        )
        if job is None:
            raise RuntimeError("原记录重生成任务未能入队")
        logger.info("draft_regeneration_enqueue_finished run_id=%s draft_id=%s job_id=%s", run_id, draft_id, job.job_id)
        return job.job_id
    finally:
        await pool.aclose()


async def enqueue_general_chat_job(
    settings: Settings,
    run_id: str,
    session_id: str,
    content: str,
    attachment_id: str | None = None,
    auto_review: bool = False,
    auto_illustration: bool = False,
) -> str:
    """对话派发独立入队：HTTP 只写回执，意图识别与工具执行都在 Worker 里完成。"""
    logger.info(
        "general_chat_enqueue_started run_id=%s session_id=%s content_length=%s",
        run_id, session_id, len(content),
    )
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job(
            "process_general_chat_job", run_id, session_id, content,
            attachment_id, auto_review, auto_illustration,
        )
        if job is None:
            raise RuntimeError("会话回复任务未能入队")
        logger.info("general_chat_enqueue_finished run_id=%s job_id=%s", run_id, job.job_id)
        return job.job_id
    finally:
        await pool.aclose()


async def enqueue_draft_revision_job(
    settings: Settings,
    draft_id: str,
    review_id: str,
    chat_run_id: str | None = None,
    extra_issues: list[str] | None = None,
) -> str:
    """按审核意见改稿独立入队：一次内容模型调用要几分钟，不能占着对话请求。"""
    logger.info(
        "draft_revision_enqueue_started draft_id=%s review_id=%s chat_run_id=%s extra_issues=%s",
        draft_id, review_id or "-", chat_run_id or "-", len(extra_issues or []),
    )
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job(
            "process_draft_revision_job", draft_id, review_id, chat_run_id, extra_issues or []
        )
        if job is None:
            raise RuntimeError("改稿任务未能入队")
        logger.info("draft_revision_enqueue_finished draft_id=%s job_id=%s", draft_id, job.job_id)
        return job.job_id
    finally:
        await pool.aclose()


async def enqueue_image_generation_job(settings: Settings, image_task_id: str) -> str:
    """图片任务和采集任务分离，返回 ARQ ID 供审计而非暴露 Redis 细节给前端。"""
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job("process_image_generation_job", image_task_id)
        if job is None:
            raise RuntimeError("图片后台任务未能入队")
        logger.info("image_generation_enqueue_finished image_task_id=%s job_id=%s", image_task_id, job.job_id)
        return job.job_id
    finally:
        await pool.aclose()


async def enqueue_wechat_delivery_job(
    settings: Settings, draft_id: str, chat_run_id: str | None = None,
) -> str:
    """公众号草稿投递/更新入队：上传图片与创建或覆盖草稿都很慢，不能占着对话请求。"""
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job("process_wechat_delivery_job", draft_id, chat_run_id)
        if job is None:
            raise RuntimeError("公众号投递任务未能入队")
        logger.info("wechat_delivery_enqueue_finished draft_id=%s chat_run_id=%s job_id=%s", draft_id, chat_run_id, job.job_id)
        return job.job_id
    finally:
        await pool.aclose()


async def enqueue_auto_review_job(
    settings: Settings, draft_id: str, review_id: str, deliver: bool = True,
    chat_run_id: str | None = None,
) -> str:
    """人工点击审核仅入队；模型与公众号投递不占用 HTTP 请求。

    `deliver=False` 表示“仅审核”：跑规则与模型审核并按意见改稿一轮，但不创建公众号草稿。
    `chat_run_id` 用于把这次审核挂到一条对话运行上，使其在“生成记录”里可见。
    """
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job("process_auto_review_job", draft_id, review_id, deliver, chat_run_id)
        if job is None:
            raise RuntimeError("自动审核后台任务未能入队")
        logger.info(
            "auto_review_enqueue_finished draft_id=%s review_id=%s deliver=%s job_id=%s",
            draft_id, review_id, deliver, job.job_id,
        )
        return job.job_id
    finally:
        await pool.aclose()


async def enqueue_collection_finalizer(
    settings: Settings, chat_run_id: str, response_message_id: str | None,
) -> str:
    """图片子任务结束后触发一次幂等收束；重复入队由 Worker 审计事件去重。"""
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job("process_collection_finalizer", chat_run_id, response_message_id)
        if job is None:
            raise RuntimeError("后台收束任务未能入队")
        return job.job_id
    finally:
        await pool.aclose()
