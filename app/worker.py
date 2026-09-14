from __future__ import annotations

import asyncio
import httpx
import logging
from datetime import UTC, datetime

from arq.connections import RedisSettings
from arq.worker import func

from app.agents.content_main_agent import ContentMainAgent
from app.agents.content_deep_agent import ContentDeepAgent
from app.config import Settings
from app.domain.models import (
    AgentCollectCommand,
    ConversationIntent,
    ConversationRunStatus,
    RawSourceItem,
    SourceKind,
)
from app.jobs import enqueue_collection_finalizer, enqueue_image_generation_job
from app.observability import configure_observability
from app.services.auto_delivery import auto_review_and_create_wechat_draft
from app.services.chat_dispatch import ChatDispatchContext, dispatch_chat_message
from app.services.reply_stream import streaming_run
from app.services.generator import GenerationError, build_generator
from app.services.model_errors import sanitize_failure_text
from app.services.source_snapshots import DraftSourceSnapshotStore
from app.services.task_narration import compose_task_reply
from app.services.runtime_settings import load_runtime_settings
from app.storage.database import SessionLocal
from app.storage.repositories import RepositoryError, ContentRepository
from app.storage.tables import ChatAgentRunRow
from app.tools.illustration_planner import (
    IllustrationPlanner,
    _paragraphs,
    style_for,
    subject_for,
    visual_direction_for,
)
from app.tools.image_generation import ImageGenerationError, ImageGenerationTool
from app.tools.source_tools import build_source_tools
from app.workflows.content_workflow import ContentPipeline


settings = Settings()
configure_observability(settings)
logger = logging.getLogger("news_agent.worker")


def collection_completion(result) -> tuple[ConversationRunStatus, str, str]:
    item_errors = [
        error.get("error", "")
        for run in getattr(result, "runs", [])
        for error in run.get("errors", [])
        if isinstance(error, dict)
    ]
    if result.created == 0 and item_errors:
        # 兜底脱敏：历史审计里可能仍存有供应商原始响应，这里绝不回显。
        detail = sanitize_failure_text(item_errors[0]) or "内容模型调用失败，请查看生成记录"
        return ConversationRunStatus.FAILED, f"生成失败：{detail}", "failed"
    if result.status.value == "failed":
        first_error = result.source_errors[0] if result.source_errors else {}
        source = first_error.get("source", "资讯来源")
        detail = sanitize_failure_text(first_error.get("error", "")) or "未返回具体失败原因"
        return ConversationRunStatus.FAILED, f"采集失败：{source} 来源请求失败。{detail}", "failed"
    prefix = "部分采集完成" if result.status.value == "partial" else "采集完成"
    targeted = [
        run.get("selection", {}).get("target")
        for run in getattr(result, "runs", [])
        if isinstance(run.get("selection"), dict) and run["selection"].get("target")
    ]
    if targeted:
        name = targeted[0]
        if result.created:
            return ConversationRunStatus.COMPLETED, f"已按指定项目 {name} 生成 1 条待审核草稿", "completed"
        summary = (
            f"指定项目 {name} 未生成草稿：该项目已有草稿或已发布，请用“重新生成”更新原有草稿。"
            if not item_errors
            else f"指定项目 {name} 生成失败，详情见执行摘要。"
        )
        status = ConversationRunStatus.FAILED if item_errors else ConversationRunStatus.COMPLETED
        return status, summary, "failed" if item_errors else "completed"
    summary = f"{prefix}：新增 {result.created} 条待审核草稿"
    if result.source_errors:
        failed_sources = "、".join(item.get("source", "未知来源") for item in result.source_errors)
        summary += f"；{failed_sources} 采集失败，详情见执行摘要。"
    return ConversationRunStatus.COMPLETED, summary, "completed"


def collection_memory_summary(result, summary: str) -> str:
    """将本轮可审计结果压缩到当前会话，零新增时也能回答追问。"""
    runs = getattr(result, "runs", [])
    duplicates = sum(run.get("duplicates", 0) for run in runs)
    skipped = sum(run.get("skipped", 0) for run in runs)
    if result.created:
        return summary
    reasons: list[str] = []
    if duplicates:
        reasons.append(f"其中 {duplicates} 条已存在未废弃草稿或已发布记录，已按去重规则跳过")
    if skipped:
        reasons.append(f"{skipped} 条未进入生成候选")
    if not reasons:
        reasons.append("没有符合生成条件的新候选")
    # 与失败提示同理：summary 可能已以句号结尾，拼接前先去掉句末标点。
    base = summary.strip().rstrip("。；;. ")
    return f"{base}；{'；'.join(reasons)}。"


def image_tasks_need_manual_attention(tasks) -> bool:
    """任一配图失败时保留文字草稿，但不能自动审核通过或投递。"""
    return any(task.status in {"failed", "timed_out"} for task in tasks)


def _raw_source_from_draft(draft) -> RawSourceItem:
    source = draft.source_item
    return RawSourceItem(
        source_kind=SourceKind(source.source_kind),
        external_id=source.external_id,
        title=source.title,
        url=source.url,
        author=source.author,
        published_at=source.published_at or datetime.now(UTC),
        summary=source.summary,
        content=source.content,
        source_name=source.source_name,
        metrics=source.metrics_json or {},
        metadata=source.metadata_json or {},
    )


def _finish_failed_run(chat_run_id: str, response_message_id: str, detail: str, title: str) -> None:
    """取消与普通异常都必须结束父运行，避免前端无限显示生成中。

    运行记录可能已被运营者在“生成记录”里删除：此时收尾只能放弃写入，
    **不能因为找不到记录再抛一次异常**——那会把真正的失败原因掩盖掉
    （真实故障：采集并发冲突的 IntegrityError 被收尾时的 RepositoryError 顶替）。
    """
    with SessionLocal() as session:
        repository = ContentRepository(session)
        try:
            repository.update_chat_message(response_message_id, detail)
            repository.add_chat_agent_event(chat_run_id, title, detail, "failed", metadata={"phase": "text", "state": "failed"})
            repository.finish_chat_agent_run(chat_run_id, response_message_id, ConversationRunStatus.FAILED, "处理失败", [], detail)
            repository.upsert_failure_notification(
                "chat_agent_run", chat_run_id, "generation", "文案生成失败", detail, "review",
            )
            session.commit()
        except RepositoryError as exc:
            session.rollback()
            logger.warning(
                "failed_run_finish_skipped chat_run_id=%s error_type=%s detail=%s",
                chat_run_id, type(exc).__name__, detail[:120],
            )


async def _compose_report(
    run_id: str | None, *, task: str, facts: dict, fallback: str
) -> str:
    """任务汇报：开启流式时边生成边推送（SSE），失败自动回退非流式。

    结构化路径（生成/审核/改稿）不受影响：它们要求完整 JSON，无法边生成边用。
    """
    from app.services.reply_stream import StreamPublisher, stream_enabled
    from app.services.task_narration import compose_task_reply_streaming

    if not run_id or not stream_enabled():
        return await asyncio.to_thread(
            compose_task_reply, settings, task=task, facts=facts, fallback=fallback, run_id=run_id or ""
        )
    publisher = StreamPublisher(run_id, "report")
    reply = await asyncio.to_thread(
        compose_task_reply_streaming,
        settings,
        task=task,
        facts=facts,
        fallback=fallback,
        run_id=run_id,
        on_delta=publisher.delta,
    )
    publisher.done(reply, status="completed")
    return reply


def _generation_failure_detail(exc: Exception, *, preserved_draft: bool = False) -> str:
    """面向运营人员显示安全失败原因，不泄露供应商响应或密钥。"""
    if isinstance(exc, GenerationError):
        detail = str(exc)
    else:
        detail = "内容生成执行失败，请查看生成记录后重试"
    # 去掉句末标点再拼接：否则会出现“……后重试。；未生成……”这种句号后跟分号的瑕疵。
    base = detail.strip().rstrip("。；;. ")
    suffix = "原草稿与历史版本未被覆盖。" if preserved_draft else "未生成新的草稿。"
    return f"{base}；{suffix}"


def _regenerate_draft_in_thread(settings: Settings, draft_id: str, raw) -> dict:
    """在线程里用独立会话重新生成草稿，避免阻塞事件循环与嵌套事件循环。

    这里**必须显式提交**：`regenerate_draft` 只 flush 不 commit，而 `with SessionLocal()`
    退出时会把未提交的改动回滚——真实故障：运行报告“已更新原草稿至版本 2”，
    但数据库里还是版本 1（用户看到“重写完了但文章没变”）。
    """
    with SessionLocal() as session:
        result = ContentPipeline(session, build_generator(settings), settings).regenerate_draft(draft_id, raw)
        session.commit()
        return result


async def process_collection_job(
    _ctx: dict,
    chat_run_id: str,
    session_id: str,
    response_message_id: str,
    source_names: list[str],
    limit: int,
    auto_review_requested: bool = False,
    auto_illustration_requested: bool = False,
    target: str | None = None,
) -> None:
    """只生成文字与建立图片子任务，不等待慢速生图。"""
    logger.info(
        "collection_job_started chat_run_id=%s sources=%s limit=%s target=%s",
        chat_run_id,
        source_names,
        limit,
        target or "-",
    )
    try:
        job_settings = load_runtime_settings(settings)
        timeout = httpx.Timeout(job_settings.request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            agent = ContentMainAgent(
                build_source_tools(client, job_settings), build_generator(job_settings), settings=job_settings
            )
            # 正文生成是全程最慢的一步（一次调用 9–12 分钟）：把 body 边写边推给对话页，
            # 用户能看到“正在写正文”，而不是长时间黑箱等待。流式失败只影响可见性，不影响生成。
            with streaming_run(chat_run_id, "draft"):
                result = await agent.run(
                    AgentCollectCommand(
                        sources=[SourceKind(name) for name in source_names],
                        limit=limit,
                        target=target,
                    )
                )
        conversation_status, summary, event_status = collection_completion(result)
        draft_ids = [draft_id for source_run in getattr(result, "runs", []) for draft_id in source_run.get("created_draft_ids", [])]
        image_task_ids: list[str] = []
        with SessionLocal() as session:
            repository = ContentRepository(session)
            repository.add_chat_agent_event(
                chat_run_id,
                "文字生成完成" if conversation_status != ConversationRunStatus.FAILED else "文字生成失败",
                summary,
                event_status,
                metadata={"phase": "text", "state": event_status, "draft_count": result.created, "draft_ids": draft_ids, "collection_result": result.model_dump(mode="json")},
            )
            if conversation_status == ConversationRunStatus.FAILED:
                repository.update_chat_message(response_message_id, summary)
                repository.finish_chat_agent_run(chat_run_id, response_message_id, conversation_status, summary, [result.model_dump(mode="json")], summary)
                repository.upsert_failure_notification(
                    "chat_agent_run", chat_run_id, "generation", "文案生成失败", summary, "review",
                )
                session.commit()
                return
            if len(draft_ids) == 1:
                repository.update_chat_session_memory(
                    session_id,
                    active_draft_id=draft_ids[0],
                    summary="本会话最近生成了一篇待审核草稿。",
                )
            elif draft_ids:
                repository.update_chat_session_memory(
                    session_id,
                    summary="本会话最近生成了多篇草稿；后续配图前请明确选择文章。",
                    clear_draft=True,
                )
            else:
                # 零新增同样是用户可追问的工作结果；保留原活动草稿，不把它误清空。
                repository.update_chat_session_memory(
                    session_id,
                    summary=collection_memory_summary(result, summary),
                )
            if auto_illustration_requested:
                for draft_id in draft_ids:
                    draft = repository.get_draft(draft_id)
                    plan = IllustrationPlanner(settings).decide(draft)
                    for index, position in enumerate(plan.placements):
                        subject = plan.subjects[index] if index < len(plan.subjects) else ""
                        style = plan.styles[index] if index < len(plan.styles) else ""
                        image_task_ids.append(
                            repository.create_image_generation_job(
                                chat_run_id, draft_id, "inline", position, subject, style
                            ).id
                        )
            # 自动审核需要封面时预先在私有库生成，仍不会发表或上传公众号。
            if auto_review_requested:
                for draft_id in draft_ids:
                    draft = repository.get_draft(draft_id)
                    image_task_ids.append(
                        repository.create_image_generation_job(
                            chat_run_id, draft_id, "cover", 0,
                            subject_for(draft, "cover"), style_for(draft, "cover"),
                        ).id
                    )
            if image_task_ids:
                repository.add_chat_agent_event(chat_run_id, "配图任务已入队", f"已创建 {len(image_task_ids)} 个独立图片任务；文字草稿可先在生成记录中查看。", "running", metadata={"phase": "image", "state": "queued", "task_ids": image_task_ids})
            else:
                if (auto_illustration_requested or auto_review_requested) and not draft_ids:
                    repository.add_chat_agent_event(chat_run_id, "未创建图片任务", "本次采集没有新增草稿，因此未创建图片任务。", metadata={"phase": "image", "state": "skipped"})
                else:
                    repository.add_chat_agent_event(chat_run_id, "未启用自动配图", "本次任务未勾选自动配图。", metadata={"phase": "image", "state": "disabled"})
            if draft_ids or image_task_ids:
                # 任务结果交给模型写汇报与追问；模型不可用时回落到确定性文案。
                facts = {
                    "task": "采集并生成草稿",
                    "drafts_created": len(draft_ids),
                    "draft_ids": draft_ids,
                    "auto_review_requested": auto_review_requested,
                    "auto_review_state": "未勾选，尚未运行" if not auto_review_requested else "已勾选，将由后台继续",
                    "image_tasks_created": len(image_task_ids),
                    "auto_illustration_requested": auto_illustration_requested,
                    "conversation_status": conversation_status.value,
                    "summary": summary,
                }
                fallback = (
                    f"草稿已生成（{len(draft_ids)} 条待审核）。要现在运行一次自动审核吗？回复“审核”即可；也可以先看文案再说。"
                    if draft_ids and not auto_review_requested
                    else summary
                )
                reply = await asyncio.to_thread(
                    compose_task_reply, settings, task="采集并生成草稿", facts=facts,
                    fallback=fallback, run_id=chat_run_id,
                )
                repository.create_chat_message(session_id, "assistant", reply)
                repository.add_chat_agent_event(
                    chat_run_id, "已汇报任务结果",
                    "已根据任务结果生成对话汇报（是否追问由模型按结果决定）。",
                    "completed",
                    metadata={"phase": "review", "state": "pending" if draft_ids and not auto_review_requested else "reported", "draft_ids": draft_ids},
                )
            session.commit()
        for image_task_id in image_task_ids:
            arq_job_id = await enqueue_image_generation_job(settings, image_task_id)
            with SessionLocal() as session:
                repository = ContentRepository(session)
                repository.update_image_generation_job(image_task_id, "queued", arq_job_id=arq_job_id)
                session.commit()
        if not image_task_ids:
            await enqueue_collection_finalizer(settings, chat_run_id, response_message_id)
        logger.info("collection_job_text_finished chat_run_id=%s image_tasks=%s", chat_run_id, len(image_task_ids))
    except asyncio.CancelledError:
        logger.warning("collection_job_timed_out chat_run_id=%s", chat_run_id)
        _finish_failed_run(chat_run_id, response_message_id, "后台任务处理超时，已停止。请重新发起任务。", "执行超时")
        raise
    except Exception as exc:
        logger.exception("collection_job_failed chat_run_id=%s error_type=%s", chat_run_id, type(exc).__name__)
        _finish_failed_run(chat_run_id, response_message_id, _generation_failure_detail(exc), "文字生成失败")
        raise


async def process_draft_regeneration_job(
    _ctx: dict,
    chat_run_id: str,
    session_id: str,
    response_message_id: str,
    draft_id: str,
    auto_review_requested: bool = False,
    auto_illustration_requested: bool = False,
) -> None:
    """重用原 ChatAgentRun 与 Draft；仅更新可编辑文案版本。"""
    logger.info("draft_regeneration_started run_id=%s draft_id=%s", chat_run_id, draft_id)
    try:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft = repository.get_draft(draft_id)
            raw = _raw_source_from_draft(draft)
            snapshots = DraftSourceSnapshotStore(settings, repository)
            cached = snapshots.restore_github_readme(draft_id, raw)
            if cached is None:
                # 兼容本次改造前已成功保存到 source_items 的 README，先转存再生成。
                snapshots.capture_github_readme(draft_id, raw)
                cached = snapshots.restore_github_readme(draft_id, raw)
        if cached is not None:
            raw = cached
            logger.info("draft_regeneration_using_source_snapshot run_id=%s draft_id=%s", chat_run_id, draft_id)
        else:
            timeout = httpx.Timeout(settings.request_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                tools = build_source_tools(client, settings)
                tool = tools.get(raw.source_kind)
                if tool is None:
                    raise ValueError(f"当前草稿来源 {raw.source_kind.value} 不支持重新获取")
                refreshed = await tool.enrich_items([raw])
                raw = refreshed[0] if refreshed else raw
        with SessionLocal() as session:
            repository = ContentRepository(session)
            # 与采集任务保持一致：同步生成放到线程里执行，避免阻塞事件循环（否则生成期间的
            # 健康检查、图片任务与 ARQ 超时都会被卡住），并让内部检索在独立线程的循环里运行。
            with streaming_run(chat_run_id, "draft"):
                regenerated = await asyncio.to_thread(
                    _regenerate_draft_in_thread, settings, draft_id, raw
                )
            repository.update_chat_session_memory(
                session_id, active_draft_id=draft_id,
                summary="本会话当前草稿已在原生成记录内重新生成。",
            )
            repository.add_chat_agent_event(
                chat_run_id, "文字重新生成完成",
                f"已更新原草稿至版本 {regenerated['version']}，未创建新的草稿或生成记录。",
                metadata={"phase": "text", "state": "completed", "draft_ids": [draft_id], "regeneration": True, **regenerated},
            )
            image_task_ids: list[str] = []
            if auto_illustration_requested:
                removed = repository.remove_generated_draft_illustrations(draft_id)
                invalidated_delivery = repository.invalidate_unfinished_wechat_publication_for_regeneration(draft_id)
                if removed:
                    repository.add_chat_agent_event(
                        chat_run_id,
                        "已替换旧生成配图",
                        f"已从当前草稿移除 {removed} 张旧的系统生成图片；手动上传图片保持不变。",
                        metadata={"phase": "image", "state": "replacing", "removed_count": removed, "regeneration": True},
                    )
                if invalidated_delivery:
                    repository.add_chat_agent_event(
                        chat_run_id,
                        "旧草稿箱素材已失效",
                        "文案与系统配图已重新生成；下一次草稿箱投递会重新选择当前图片。",
                        metadata={"phase": "delivery", "state": "superseded", "regeneration": True},
                    )
                updated = repository.get_draft(draft_id)
                plan = IllustrationPlanner(settings).decide(updated)
                for index, position in enumerate(plan.placements):
                    subject = plan.subjects[index] if index < len(plan.subjects) else ""
                    style = plan.styles[index] if index < len(plan.styles) else ""
                    image_task_ids.append(
                        repository.create_image_generation_job(
                            chat_run_id, draft_id, "inline", position, subject, style
                        ).id
                    )
            if auto_review_requested and not any(item.purpose == "cover" for item in repository.list_draft_illustrations(draft_id)):
                cover_draft = repository.get_draft(draft_id)
                image_task_ids.append(
                    repository.create_image_generation_job(
                        chat_run_id, draft_id, "cover", 0,
                        subject_for(cover_draft, "cover"), style_for(cover_draft, "cover"),
                    ).id
                )
            if image_task_ids:
                repository.add_chat_agent_event(chat_run_id, "配图任务已入队", f"原记录已创建 {len(image_task_ids)} 个图片任务。", "running", metadata={"phase": "image", "state": "queued", "task_ids": image_task_ids, "regeneration": True})
            else:
                repository.add_chat_agent_event(chat_run_id, "复用既有配图", "本次未请求新配图，原草稿图片保持不变。", metadata={"phase": "image", "state": "completed", "regeneration": True})
            if not image_task_ids:
                # 重新生成会作废旧投递素材选择：把结果交给模型，由它决定怎么说明与是否追问。
                existing = repository.list_draft_illustrations(draft_id)
                facts = {
                    "task": "按已保存证据重写/重新生成正文",
                    "draft_id": draft_id,
                    "new_version": repository.get_draft(draft_id).version,
                    "existing_illustrations": len(existing),
                    "new_image_tasks": 0,
                    "publication_asset_selection": "已作废，需要重新确定或复用",
                    "auto_review_requested": auto_review_requested,
                }
                fallback = (
                    f"正文已更新为版本 {facts['new_version']}，原有 {len(existing)} 张配图仍保留。"
                    "要直接复用这些配图，还是按新正文重新生成配图？回复“复用”或“重新生成配图”即可。"
                    if existing
                    else f"正文已更新为版本 {facts['new_version']}；当前没有配图，需要的话我可以按新正文生成配图。"
                )
                reply = await asyncio.to_thread(
                    compose_task_reply, settings, task="按已保存证据重写正文", facts=facts,
                    fallback=fallback, run_id=chat_run_id,
                )
                repository.create_chat_message(session_id, "assistant", reply)
                repository.add_chat_agent_event(
                    chat_run_id, "已汇报任务结果",
                    "已根据重写结果生成对话汇报（是否追问由模型按结果决定）。",
                    "completed",
                    metadata={"phase": "image", "state": "pending" if existing else "reported", "draft_ids": [draft_id], "regeneration": True},
                )
            session.commit()
        for image_task_id in image_task_ids:
            arq_job_id = await enqueue_image_generation_job(settings, image_task_id)
            with SessionLocal() as session:
                ContentRepository(session).update_image_generation_job(image_task_id, "queued", arq_job_id=arq_job_id)
                session.commit()
        if not image_task_ids:
            await enqueue_collection_finalizer(settings, chat_run_id, response_message_id)
    except asyncio.CancelledError:
        _finish_failed_run(chat_run_id, response_message_id, "原记录重生成超时，原草稿未被覆盖。", "原记录重生成超时")
        raise
    except Exception as exc:
        logger.exception("draft_regeneration_failed run_id=%s draft_id=%s error_type=%s", chat_run_id, draft_id, type(exc).__name__)
        _finish_failed_run(chat_run_id, response_message_id, _generation_failure_detail(exc, preserved_draft=True), "原记录重生成失败")
        raise


async def process_general_chat_job(
    _ctx: dict,
    chat_run_id: str,
    session_id: str,
    content: str,
    attachment_id: str | None = None,
    auto_review: bool = False,
    auto_illustration: bool = False,
) -> None:
    """对话派发：HTTP 只保存用户消息并写回执，这里才做意图识别与工具执行。

    运行在受理时已经建好（含一条确定性回执消息）：这里只负责推进它，
    结束时把最终回复**追加**为新消息，运行卡片随之显示完成状态。
    """
    logger.info(
        "general_chat_job_started run_id=%s session_id=%s content_length=%s",
        chat_run_id, session_id, len(content),
    )
    outcome = None
    try:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            run = repository.get_chat_agent_run(chat_run_id)
            if run.status != ConversationRunStatus.RUNNING.value:
                logger.info("general_chat_job_skipped run_id=%s status=%s", chat_run_id, run.status)
                return
            ctx = ChatDispatchContext(
                settings=settings,
                repository=repository,
                session=session,
                session_id=session_id,
                run_id=chat_run_id,
                content=content,
                attachment_id=attachment_id,
                auto_review=auto_review,
                auto_illustration=auto_illustration,
            )
            outcome = await dispatch_chat_message(ctx)
            if outcome.keep_running:
                if outcome.summary:
                    run.summary = outcome.summary
                session.commit()
                logger.info("general_chat_job_handed_off run_id=%s summary=%s", chat_run_id, outcome.summary)
                return
            message_id = run.response_message_id
            if outcome.reply:
                message_id = repository.append_run_reply(chat_run_id, outcome.reply)
            repository.finish_chat_agent_run(
                chat_run_id, message_id, outcome.status, outcome.summary,
                outcome.results, outcome.error,
            )
            session.commit()
    except Exception as exc:
        logger.exception("general_chat_job_failed run_id=%s error_type=%s", chat_run_id, type(exc).__name__)
        anchor = None
        try:
            with SessionLocal() as session:
                anchor = ContentRepository(session).get_chat_agent_run(chat_run_id).response_message_id
        except RepositoryError:
            logger.warning("general_chat_failed_run_missing run_id=%s", chat_run_id)
        _finish_failed_run(chat_run_id, anchor, str(exc), "对话派发失败")
        return
    logger.info(
        "general_chat_job_finished run_id=%s status=%s",
        chat_run_id, outcome.status.value if outcome else "unknown",
    )


async def process_auto_review_job(
    _ctx: dict,
    draft_id: str,
    review_id: str,
    deliver: bool = True,
    chat_run_id: str | None = None,
    revise: bool = True,
) -> None:
    """在 Worker 中完成耗时审核、（可选）一轮自动改稿，并按需受控投递草稿箱。

    `revise=False` 时只出审核意见、不改稿——用户要“先看看审核怎么说”时走这条。
    """
    logger.info(
        "auto_review_job_started draft_id=%s review_id=%s deliver=%s revise=%s chat_run_id=%s",
        draft_id, review_id, deliver, revise, chat_run_id or "-",
    )
    with SessionLocal() as session:
        repository = ContentRepository(session)
        run = repository.get_auto_review_run(review_id)
        if run.draft_id != draft_id or run.status not in {"queued", "running"}:
            logger.info("auto_review_job_skipped draft_id=%s review_id=%s status=%s", draft_id, review_id, run.status)
            return
        repository.mark_auto_review_run_running(review_id)
        if chat_run_id:
            # 让这次审核在“生成记录”里有可见的运行条目与阶段状态。
            repository.add_chat_agent_event(
                chat_run_id, "自动审核中",
                "正在按规则与模型审核当前文案与配图，并按意见改稿一轮。"
                if revise
                else "正在按规则与模型审核当前文案与配图；按你的要求只出意见，不改稿。",
                "running",
                metadata={"phase": "review", "state": "running", "draft_ids": [draft_id], "review_id": review_id},
            )
        session.commit()
    try:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            result = await auto_review_and_create_wechat_draft(
                settings, repository, draft_id, chat_agent_run_id=chat_run_id,
                review_run_id=review_id, deliver=deliver, revise=revise,
            )
            session.commit()
        logger.info("auto_review_job_finished draft_id=%s review_id=%s status=%s", draft_id, review_id, result.get("status"))
        if chat_run_id:
            await _report_auto_review_result(chat_run_id, draft_id, result)
    except asyncio.CancelledError:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            repository.finish_auto_review_run(review_id, "failed", {}, {}, "自动审核后台任务超时，已停止。")
            if chat_run_id:
                repository.finish_chat_agent_run(chat_run_id, None, ConversationRunStatus.FAILED, "自动审核超时，已停止。", [])
            session.commit()
        logger.warning("auto_review_job_timed_out draft_id=%s review_id=%s", draft_id, review_id)
        raise
    except Exception as exc:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            repository.finish_auto_review_run(review_id, "failed", {}, {}, "自动审核后台任务失败，请稍后重试。")
            if chat_run_id:
                repository.finish_chat_agent_run(chat_run_id, None, ConversationRunStatus.FAILED, "自动审核失败，请稍后重试。", [])
            session.commit()
        logger.exception("auto_review_job_failed draft_id=%s review_id=%s error_type=%s", draft_id, review_id, type(exc).__name__)
        raise


async def _report_auto_review_result(chat_run_id: str, draft_id: str, result: dict) -> None:
    """审核结束：结束运行，并让模型根据真实审核结果写汇报（含是否追问）。"""
    with SessionLocal() as session:
        repository = ContentRepository(session)
        chat_run = repository.get_chat_agent_run(chat_run_id)
        draft = repository.get_draft(draft_id)
        status = str(result.get("status") or "")
        issues = result.get("issues") or []
        # 审核结果的状态值有多个：仅审核通过与已投递都算通过，别把 approved_no_delivery 误报成未通过。
        passed = result.get("passed")
        if passed is None:
            passed = status.startswith("approved") or status in {"delivered", "draft_created", "wechat_draft_created"}
        revise_disabled = bool(result.get("revise_disabled"))
        summary = (
            f"自动审核{'通过' if passed else '未通过'}：版本 {draft.version}；"
            f"共 {len(issues)} 条意见。"
        )
        fallback = f"自动审核{'通过' if passed else '未通过'}：当前文案版本 {draft.version}，共 {len(issues)} 条意见。"
        reply = await _compose_report(
            chat_run_id,
            task="自动审核（仅出意见，未改稿）" if revise_disabled else "自动审核（含一轮按意见改稿）",
            facts={
                "draft_title": (draft.title_options_json or ["当前文案"])[0],
                "draft_version": draft.version,
                "review_status": status or "unknown",
                "passed": passed,
                "issue_count": len(issues),
                "issues": [str(item)[:120] for item in issues[:5]],
                "delivery": "已按要求投递" if result.get("delivery") else "未投递",
                # 只审不改时必须说清“没有改稿”，否则用户会以为文章已经被改过。
                "revised": not revise_disabled,
                "next_step": "需要的话我可以按这些意见改稿（apply_revision_issues）或重新审核。"
                if revise_disabled
                else None,
            },
            fallback=fallback,
        )
        if chat_run.response_message_id:
            repository.append_run_reply(chat_run_id, reply)
        repository.finish_chat_agent_run(
            chat_run_id, chat_run.response_message_id, ConversationRunStatus.COMPLETED, summary, [result]
        )
        session.commit()


async def process_wechat_delivery_job(
    _ctx: dict, draft_id: str, chat_run_id: str | None = None, deliver: bool = True,
) -> None:
    """后台完成公众号草稿的创建或原地覆盖，并在对话里汇报结果。

    投递要上传封面与正文图片、再调用微信写接口，耗时远超对话请求时限，因此独立成后台任务。
    `deliver=False` 表示**只重新选择投递配图、不碰远端草稿**（用户想先看看选了哪几张时用）。
    """
    logger.info(
        "wechat_delivery_job_started draft_id=%s chat_run_id=%s deliver=%s",
        draft_id, chat_run_id or "-", deliver,
    )
    with SessionLocal() as session:
        repository = ContentRepository(session)
        draft = repository.get_draft(draft_id)
        existing = repository.get_wechat_publication_for_draft(draft_id)
        updating = bool(existing is not None and existing.wechat_draft_media_id)
        if chat_run_id:
            repository.add_chat_agent_event(
                chat_run_id,
                "正在重新选择投递配图" if not deliver else "正在投递公众号草稿",
                "只重新选择封面与正文插图并落库，不会创建或覆盖远端草稿。"
                if not deliver
                else "正在上传封面与正文图片并写入公众号草稿箱；已存在远端草稿时会原地覆盖。",
                "running",
                metadata={"phase": "text", "state": "running", "draft_ids": [draft_id], "delivery": deliver},
            )
        session.commit()
    try:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft = repository.get_draft(draft_id)
            from app.services.auto_delivery import (
                prepare_agent_selected_wechat_assets,
                retry_agent_selected_wechat_draft,
            )

            if deliver:
                job = await retry_agent_selected_wechat_draft(settings, repository, draft_id)
            else:
                # 只重选：走与投递同一条素材选择实现，但不创建/覆盖远端草稿。
                job = await prepare_agent_selected_wechat_assets(settings, repository, draft_id)
            session.commit()
        logger.info("wechat_delivery_job_finished draft_id=%s state=%s deliver=%s", draft_id, job.state, deliver)
        if chat_run_id:
            if deliver:
                await _report_wechat_delivery_result(chat_run_id, draft_id, updating=updating, error="")
            else:
                await _report_publication_selection_result(chat_run_id, draft_id, job)
    except asyncio.CancelledError:
        if chat_run_id:
            await _report_wechat_delivery_result(chat_run_id, draft_id, updating=updating, error="投递任务超时，已停止。")
        raise
    except Exception as exc:
        logger.exception("wechat_delivery_job_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        if chat_run_id:
            await _report_wechat_delivery_result(chat_run_id, draft_id, updating=updating, error=str(exc)[:200])
        raise


async def _report_publication_selection_result(chat_run_id: str, draft_id: str, job) -> None:
    """只重选配图时的汇报：说清选了哪几张、没有投递。"""
    with SessionLocal() as session:
        repository = ContentRepository(session)
        chat_run = repository.get_chat_agent_run(chat_run_id)
        draft = repository.get_draft(draft_id)
        title = (draft.title_options_json or ["当前草稿"])[0]
        inline_count = len(job.inline_asset_ids_json or [])
        reply = await _compose_report(
            chat_run_id,
            task="重新选择投递配图（未投递）",
            facts={
                "draft_title": title,
                "cover_asset_id": job.cover_asset_id,
                "inline_count": inline_count,
                "delivered": False,
                "note": "只重新选择并保存了投递配图，没有创建或覆盖公众号草稿",
            },
            fallback=f"已为《{title}》重新选择投递配图：封面 1 张、正文插图 {inline_count} 张；未投递。",
        )
        if chat_run.response_message_id:
            repository.append_run_reply(chat_run_id, reply)
        repository.finish_chat_agent_run(
            chat_run_id, chat_run.response_message_id, ConversationRunStatus.COMPLETED,
            f"已重新选择配图（未投递）：封面 1 张、正文 {inline_count} 张", [{"draft_id": draft_id}],
        )
        session.commit()


async def _report_wechat_delivery_result(chat_run_id: str, draft_id: str, *, updating: bool, error: str) -> None:
    """投递结束：结束运行，并让模型按真实结果写汇报。"""
    with SessionLocal() as session:
        repository = ContentRepository(session)
        chat_run = repository.get_chat_agent_run(chat_run_id)
        draft = repository.get_draft(draft_id)
        job = repository.get_wechat_publication_for_draft(draft_id)
        state = getattr(job, "state", "unknown")
        title = (draft.title_options_json or ["当前草稿"])[0]
        fallback = (
            f"投递失败：{error}" if error
            else f"{'已更新' if updating else '已创建'}公众号草稿（{title}），未提交发表。"
        )
        reply = await _compose_report(
            chat_run_id,
            task="投递到微信公众号草稿箱",
            facts={
                "draft_title": title,
                "updated_remote_draft": updating,
                "job_state": state,
                "error": error,
                "note": "只创建或更新草稿箱内容，绝不发表",
            },
            fallback=fallback,
        )
        if chat_run.response_message_id:
            repository.append_run_reply(chat_run_id, reply)
        repository.finish_chat_agent_run(
            chat_run_id, chat_run.response_message_id,
            ConversationRunStatus.FAILED if error else ConversationRunStatus.COMPLETED,
            reply[:300], [],
        )
        session.commit()


async def process_image_generation_job(_ctx: dict, image_task_id: str) -> None:
    """一张图片一个 ARQ Job，采用独立长超时，不阻塞采集任务。"""
    with SessionLocal() as session:
        repository = ContentRepository(session)
        task = repository.update_image_generation_job(image_task_id, "running")
        draft = repository.get_draft(task.draft_id)
        paragraphs = _paragraphs(draft.body)
        context = paragraphs[task.placement_after_paragraph - 1] if task.purpose == "inline" and 0 < task.placement_after_paragraph <= len(paragraphs) else ""
        repository.add_chat_agent_event(task.chat_agent_run_id, "图片生成中", f"图片任务 {task.id} 正在后台生成。", "running", metadata={"phase": "image", "state": "generating", "task_id": task.id, "draft_id": task.draft_id})
        session.commit()
    try:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            task = repository.get_image_generation_job(image_task_id)
            generated = await ImageGenerationTool(settings, repository).invoke(
                task.draft_id,
                task.purpose,
                task.placement_after_paragraph,
                context,
                visual_direction_for(task.purpose, task.placement_after_paragraph),
                task.subject or "",
                task.style or "",
            )
            repository.update_image_generation_job(task.id, "completed", illustration_id=generated.illustration_id)
            repository.add_chat_agent_event(task.chat_agent_run_id, "图片生成完成", f"图片任务 {task.id} 已私有保存。", metadata={"phase": "image", "state": "completed", "task_id": task.id, "draft_id": task.draft_id, "illustration_id": generated.illustration_id})
            session.commit()
    except asyncio.CancelledError:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            task = repository.update_image_generation_job(image_task_id, "timed_out", error_message="图片生成超时，已停止")
            repository.add_chat_agent_event(task.chat_agent_run_id, "图片生成超时", f"图片任务 {task.id} 超过执行时限，文字草稿已保留。", "failed", metadata={"phase": "image", "state": "timed_out", "task_id": task.id})
            repository.upsert_failure_notification(
                "image_generation_job", task.id, "image", "图片生成超时", "图片生成超过执行时限，文字草稿已保留。", "review",
            )
            session.commit()
        raise
    except Exception as exc:
        logger.warning("image_job_failed image_task_id=%s error_type=%s", image_task_id, type(exc).__name__)
        with SessionLocal() as session:
            repository = ContentRepository(session)
            task = repository.update_image_generation_job(image_task_id, "failed", error_message=str(exc))
            repository.add_chat_agent_event(task.chat_agent_run_id, "图片生成失败", f"图片任务 {task.id} 未生成，文字草稿已保留。", "failed", metadata={"phase": "image", "state": "failed", "task_id": task.id, "draft_id": task.draft_id})
            repository.upsert_failure_notification(
                "image_generation_job", task.id, "image", "图片生成失败", "图片任务未生成，文字草稿已保留。", "review",
            )
            session.commit()
    finally:
        with SessionLocal() as session:
            run_id = ContentRepository(session).get_image_generation_job(image_task_id).chat_agent_run_id
        await enqueue_collection_finalizer(settings, run_id, None)


def _draft_report_facts(repository: ContentRepository, draft_ids: list[str]) -> list[dict[str, Any]]:
    """汇报用事实：每篇草稿的标题/状态/版本、真实截图与 AI 配图数量、当前审核状态。

    真实反馈：“感觉这样不是很合理”——报告说“本次采集未产出草稿…自动审核未被触发”，
    实际是在**已有待审核草稿**上补了配图并已开始审核。事实齐了，模型才不会写出矛盾汇报。
    """
    facts: list[dict[str, Any]] = []
    for draft_id in draft_ids:
        try:
            draft = repository.get_draft(draft_id)
        except Exception:  # 草稿被删除时不影响收束
            continue
        illustrations = repository.list_draft_illustrations(draft_id)
        real = sum(1 for item in illustrations if not item.provider)
        active_review = repository.find_active_auto_review_run(draft_id)
        history = repository.list_auto_review_runs(draft_id)
        latest_review = active_review or (history[-1] if history else None)
        facts.append(
            {
                "draft_id": draft_id,
                "title": (draft.title_options_json or [""])[0],
                "status": draft.status,
                "version": draft.version,
                "illustrations_total": len(illustrations),
                "illustrations_from_source": real,
                "illustrations_ai_generated": len(illustrations) - real,
                "review_status": getattr(latest_review, "status", None) or "none",
                "review_active": active_review is not None,
            }
        )
    return facts


async def process_collection_finalizer(_ctx: dict, chat_run_id: str, response_message_id: str | None = None) -> None:
    """所有图片任务终态后才审核/投递；多次入队通过 finalizing 事件幂等收束。"""
    with SessionLocal() as session:
        repository = ContentRepository(session)
        run = session.get(ChatAgentRunRow, chat_run_id)
        if run is None or run.status != ConversationRunStatus.RUNNING.value:
            return
        # 一个生成记录可多次重开；仅本次尝试创建的图片任务能决定本次收束。
        tasks = repository.list_image_generation_jobs(chat_run_id, since=run.attempt_started_at)
        if any(task.status in {"queued", "running"} for task in tasks):
            return
        has_failed_images = image_tasks_need_manual_attention(tasks)
        events = repository.list_chat_agent_events(chat_run_id)
        attempt_events = [event for event in events if event.created_at >= run.attempt_started_at]
        if any(event.metadata_json.get("phase") == "finalization" for event in attempt_events):
            return
        text_event = next(
            (event for event in reversed(attempt_events) if event.metadata_json.get("phase") == "text"),
            None,
        )
        draft_ids = list((text_event.metadata_json if text_event else {}).get("draft_ids", []))
        # 纯配图运行没有“文字生成”事件：此时草稿来自图片任务本身。
        # 否则 facts 里 draft_ids 为空，汇报会写成“本次采集未产出草稿”，
        # 完全掩盖了“在已有待审核草稿上补配图”这个事实。
        if not draft_ids:
            draft_ids = list(dict.fromkeys(task.draft_id for task in tasks))
        run_intent = run.intent
        failed_image_tasks = [task for task in tasks if task.status in {"failed", "timed_out"}]
        if failed_image_tasks:
            repository.add_chat_agent_event(
                chat_run_id,
                "配图未完成",
                f"{len(failed_image_tasks)} 个图片任务失败或超时；文字草稿已保留，需人工补图后再审核。",
                "failed",
                metadata={
                    "phase": "image",
                    "state": "needs_attention",
                    "failed_task_ids": [task.id for task in failed_image_tasks],
                },
            )
        repository.add_chat_agent_event(
            chat_run_id,
            "图片任务已完成",
            "本次图片任务均已结束，正在收束后续流程。",
            "running",
            metadata={
                "phase": "finalization",
                "state": "running",
                "task_ids": [task.id for task in tasks],
                "attempt_started_at": run.attempt_started_at.isoformat(),
            },
        )
        session.commit()
    auto_results: list[dict] = []
    with SessionLocal() as session:
        repository = ContentRepository(session)
        run = session.get(ChatAgentRunRow, chat_run_id)
        if run is None:
            return
        if run.auto_review_requested and has_failed_images:
            auto_results = [{
                "status": "needs_attention",
                "reason": "存在失败或超时的图片任务，已跳过自动审核与公众号草稿投递。",
            }]
            repository.add_chat_agent_event(
                chat_run_id,
                "自动审核已中断",
                "配图不完整，自动审核与公众号草稿投递已中断；请补图后重新发起审核。",
                "failed",
                metadata={"phase": "review", "state": "needs_attention", "results": auto_results},
            )
        elif run.auto_review_requested and draft_ids:
            repository.add_chat_agent_event(chat_run_id, "自动审核中", "图片任务已结束，正在按规则审核文字与插图。", "running", metadata={"phase": "review", "state": "running"})
            session.commit()
            for draft_id in draft_ids:
                auto_results.append(await auto_review_and_create_wechat_draft(settings, repository, draft_id, chat_run_id))
                session.commit()
            repository.add_chat_agent_event(chat_run_id, "自动审核与草稿投递", "已完成自动审核；通过时最多创建公众号草稿，未提交发表。", metadata={"phase": "review", "state": "completed", "results": auto_results})
        elif run.auto_review_requested:
            repository.add_chat_agent_event(chat_run_id, "自动审核未执行", "没有可审核草稿，已跳过自动审核与公众号草稿投递。", metadata={"phase": "review", "state": "skipped"})
        else:
            repository.add_chat_agent_event(chat_run_id, "未启用自动审核", "本次任务未勾选自动审核。", metadata={"phase": "review", "state": "disabled"})
        summary = next((event.detail for event in reversed(repository.list_chat_agent_events(chat_run_id)) if event.metadata_json.get("phase") == "text"), "后台任务已完成")
        image_results = [{"task_id": task.id, "status": task.status, "illustration_id": task.illustration_id, "error": task.error_message} for task in tasks]
        # 配图终态后，才真正发起此前排队的审核（对话 Agent 在配图未完成时请求过审核）。
        from app.services.pending_reviews import launch_pending_reviews, pending_review_drafts

        queued_reviews = pending_review_drafts(repository, draft_ids)
        launched_reviews = await launch_pending_reviews(settings, repository, draft_ids)
        drafts_facts = _draft_report_facts(repository, draft_ids)
        message_id = response_message_id or run.response_message_id
        final_status = (
            ConversationRunStatus.WAITING_CONFIRMATION
            if run.auto_review_requested and has_failed_images
            else ConversationRunStatus.COMPLETED
        )
        if message_id:
            # 全流程结束：把配图与审核的真实结果交给模型写最终汇报（是否追问由模型按结果决定）。
            reply = await _compose_report(
                chat_run_id,
                task="生成配图" if run_intent == ConversationIntent.GENERATE_DRAFT_IMAGE.value else "采集→配图→自动审核全流程结束",
                facts={
                    "task_intent": run_intent,
                    "draft_ids": draft_ids,
                    # 每篇草稿的标题/状态/版本、真实截图与 AI 配图数量、当前审核状态：
                    # 报告据此说明“在已有草稿上做了什么”，不再用“未产出草稿”掩盖。
                    "drafts": drafts_facts,
                    "existing_draft_only": bool(draft_ids) and not text_event,
                    "auto_review_requested": run.auto_review_requested,
                    "auto_review_results": auto_results,
                    "auto_review_queued_after_images": queued_reviews,
                    "auto_review_launched_after_images": launched_reviews,
                    "images": {
                        "total": len(tasks),
                        "failed": sum(1 for task in tasks if task.status in {"failed", "timed_out"}),
                        "completed": sum(1 for task in tasks if task.status == "completed"),
                    },
                    "has_failed_images": has_failed_images,
                    "summary": summary,
                },
                fallback=summary,
            )
            # 完成后**新消息回复**：占位消息保留，结果作为新消息追加并前移运行指针。
            reply_message_id = repository.append_run_reply(chat_run_id, reply)
            repository.finish_chat_agent_run(chat_run_id, reply_message_id, final_status, summary, [*image_results, *auto_results])
        session.commit()
    logger.info("collection_finalized chat_run_id=%s", chat_run_id)


async def process_draft_revision_job(
    _ctx: dict,
    draft_id: str,
    review_id: str = "",
    chat_run_id: str | None = None,
    extra_issues: list[str] | None = None,
) -> None:
    """按审核意见再改一稿，并在对话里汇报（对话工具 `apply_revision_issues` 走这里）。"""
    from app.services.auto_delivery import apply_revision_from_review

    logger.info("draft_revision_job_started draft_id=%s review_id=%s chat_run_id=%s", draft_id, review_id, chat_run_id or "-")
    with SessionLocal() as session:
        repository = ContentRepository(session)
        draft = repository.get_draft(draft_id)
        title = (draft.title_options_json or ["当前草稿"])[0]
        if chat_run_id:
            repository.add_chat_agent_event(
                chat_run_id, "按审核意见改稿中",
                f"《{title}》正按上次审核意见改写正文；来源事实字段不变，不重新采集、不投递。",
                "running",
                metadata={"phase": "text", "state": "running", "draft_ids": [draft_id], "review_id": review_id},
            )
            session.commit()
    try:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            result = await apply_revision_from_review(
                settings, repository, draft_id, review_id, chat_run_id, extra_issues
            )
            repository.add_chat_agent_event(
                chat_run_id, "按审核意见改稿完成" if chat_run_id else "改稿完成",
                f"已更新《{result['draft_title']}》至版本 {result['version']}（按 {result['issue_count']} 条审核意见）。",
                "completed",
                metadata={"phase": "text", "state": "completed", "draft_ids": [draft_id], **result},
            )
            session.commit()
    except Exception as exc:
        logger.exception("draft_revision_job_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        with SessionLocal() as session:
            repository = ContentRepository(session)
            repository.add_chat_agent_event(
                chat_run_id, "按审核意见改稿失败", str(exc), "failed",
                metadata={"phase": "text", "state": "failed", "draft_ids": [draft_id]},
            )
            session.commit()
        if chat_run_id:
            await _compose_report(
                chat_run_id,
                task="按审核意见改稿",
                facts={"draft_id": draft_id, "review_id": review_id, "error": str(exc), "draft_title": title},
                fallback=f"按审核意见改稿失败：{exc}；原文未改动。",
            )
        return
    if chat_run_id:
        reply = await _compose_report(
            chat_run_id,
            task="按审核意见改稿",
            facts={
                "draft_title": result["draft_title"],
                "new_version": result["version"],
                "issues": result["issues"],
                "note": "只按审核意见改写正文，未重新采集、未投递",
            },
            fallback=f"已按审核意见把《{result['draft_title']}》改到版本 {result['version']}，可以重新审核或投递。",
        )
        with SessionLocal() as session:
            repository = ContentRepository(session)
            message_id = repository.append_run_reply(chat_run_id, reply)
            repository.finish_chat_agent_run(
                chat_run_id, message_id, ConversationRunStatus.COMPLETED,
                f"已按审核意见改到版本 {result['version']}", [result],
            )
            session.commit()
    logger.info("draft_revision_job_finished draft_id=%s version=%s", draft_id, result["version"])


class WorkerSettings:
    functions = [
        func(process_collection_job, timeout=settings.collection_job_timeout_seconds),
        func(process_draft_regeneration_job, timeout=settings.collection_job_timeout_seconds),
        func(process_draft_revision_job, timeout=settings.collection_job_timeout_seconds),
        func(process_general_chat_job, timeout=settings.conversation_agent_timeout_seconds + 15),
        func(process_auto_review_job, timeout=settings.collection_job_timeout_seconds),
        func(process_wechat_delivery_job, timeout=settings.collection_job_timeout_seconds),
        func(process_image_generation_job, timeout=settings.image_generation_job_timeout_seconds),
        func(process_collection_finalizer, timeout=settings.collection_job_timeout_seconds),
    ]
    max_jobs = 4
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
