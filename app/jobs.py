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
) -> str:
    logger.info(
        "collection_enqueue_started run_id=%s sources=%s limit=%s",
        run_id,
        sources,
        limit,
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
    settings: Settings, run_id: str, session_id: str, content: str,
) -> str:
    """普通会话回复独立入队，确保 HTTP 请求不等待外部模型。"""
    logger.info(
        "general_chat_enqueue_started run_id=%s session_id=%s content_length=%s",
        run_id, session_id, len(content),
    )
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job("process_general_chat_job", run_id, session_id, content)
        if job is None:
            raise RuntimeError("会话回复任务未能入队")
        logger.info("general_chat_enqueue_finished run_id=%s job_id=%s", run_id, job.job_id)
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


async def enqueue_auto_review_job(settings: Settings, draft_id: str, review_id: str) -> str:
    """人工点击审核仅入队；模型与公众号投递不占用 HTTP 请求。"""
    pool = await create_pool(redis_settings(settings))
    try:
        job = await pool.enqueue_job("process_auto_review_job", draft_id, review_id)
        if job is None:
            raise RuntimeError("自动审核后台任务未能入队")
        logger.info("auto_review_enqueue_finished draft_id=%s review_id=%s job_id=%s", draft_id, review_id, job.job_id)
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
