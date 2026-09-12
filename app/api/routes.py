from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.agents.content_main_agent import AgentCommandError, ContentMainAgent
from app.agents.content_deep_agent import ContentDeepAgent
from app.agents.chat_agent import ChatAgent
from app.domain.models import (
    AgentCollectCommand,
    AttachmentStatus,
    ChatMessageCreate,
    ChatSessionCreate,
    DraftEdit,
    DraftIllustrationCreate,
    ManualPublicationCommand,
    ConversationIntent,
    ConversationRunStatus,
    PlanConfirmationCommand,
    RawSourceItem,
    ReviewCommand,
    ReviewStatus,
    SourceKind,
    normalize_github_target,
)
from app.services.attachments import (
    AttachmentAccessError,
    AttachmentError,
    AttachmentLinkSigner,
    AgentWorkspaceStore,
    PrivateAttachmentStore,
    validate_image_attachment,
)
from app.services.plain_text import normalize_wechat_description
from app.services.review_feedback import normalized_review_report
from app.services.source_snapshots import DraftSourceSnapshotStore
from app.services.wechat_official import WechatOfficialAccountError, render_wechat_html
from app.services.generator import build_generator
from app.services.auto_delivery import (
    prepare_agent_selected_wechat_assets,
    retry_agent_selected_wechat_draft,
)
from app.tools.auto_review import AutoReviewTool
from app.tools.image_generation import ImageGenerationError, ImageGenerationTool
from app.tools.illustration_planner import PublicationAssetSelectionError
from app.tools.wechat_official_account import WechatOfficialAccountTool
from app.jobs import enqueue_auto_review_job, enqueue_collection_job, enqueue_draft_regeneration_job
from app.storage.repositories import (
    AttachmentNotFound,
    ContentRepository,
    DraftNotFound,
    PublicationAssetNotFound,
    RepositoryError,
    WechatPublicationNotFound,
)
from app.storage.database import get_session
from app.storage.tables import (
    AgentRunRow,
    AttachmentRow,
    ChatMessageRow,
    ChatAgentRunRow,
    ChatSessionRow,
    CollectionRunRow,
    DraftRow,
    ImageGenerationJobRow,
    NotificationRow,
    PublicationAssetRow,
    WechatPublicationJobRow,
    SourceItemRow,
    TrendingSnapshotRow,
)
from app.tools.plan_tools import (
    ConfirmPlanRequest,
    ConfirmPublishPlanTool,
    ConfirmSchedulePlanTool,
)
from app.tools.source_tools import build_source_tools
from app.tools.attachment_tools import ExtractTextAttachmentTool
from app.tools.files.workspace import SessionWorkspaceTool
from app.tools.scripts.registry import RegisteredScriptError, run_registered_script
from app.workflows.content_workflow import ContentPipeline

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
logger = logging.getLogger("news_agent.chat")


def source_to_dict(row: SourceItemRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_kind": row.source_kind,
        "external_id": row.external_id,
        "source_name": row.source_name,
        "title": row.title,
        "url": row.url,
        "author": row.author,
        "published_at": row.published_at,
        "category": row.category,
        "category_confidence": row.category_confidence,
        "hot_score": row.hot_score,
        "duplicate_of_id": row.duplicate_of_id,
        "metrics": row.metrics_json,
        "metadata": row.metadata_json,
    }


def draft_to_dict(row: DraftRow, settings: Settings | None = None) -> dict[str, Any]:
    source_url = row.source_url
    attachment_id = row.source_item.metadata_json.get("attachment_id")
    if (
        settings is not None
        and row.source_item.source_kind == SourceKind.ATTACHMENT.value
        and isinstance(attachment_id, str)
    ):
        source_url = (
            f"{settings.app_base_url}/api/attachments/{attachment_id}/download?token="
            f"{AttachmentLinkSigner(settings).issue(attachment_id)}"
        )
    return {
        "id": row.id,
        "source_item_id": row.source_item_id,
        "status": row.status,
        "title_options": row.title_options_json,
        "summary_cn": row.summary_cn,
        "body": row.body,
        "tags": row.tags_json,
        "card_script": row.card_script_json,
        "source_name": row.source_name,
        "source_url": source_url,
        "evidence": row.evidence_json,
        "content_plan": row.content_plan_json,
        "quality_report": row.quality_report_json,
        "claim_citations": row.claim_citations_json,
        "generation_mode": row.generation_mode,
        "published_platform": row.published_platform,
        "published_url": row.published_url,
        "published_at": row.published_at,
        "category": row.source_item.category,
        "version": row.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def draft_illustration_to_dict(row: Any, repository: ContentRepository, settings: Settings) -> dict[str, Any]:
    asset = repository.get_publication_asset(row.asset_id)
    return {
        "id": row.id, "draft_id": row.draft_id, "asset_id": row.asset_id,
        "purpose": row.purpose, "placement_after_paragraph": row.placement_after_paragraph,
        "prompt": row.prompt, "provider": row.provider, "model": row.model,
        "created_at": row.created_at, "asset": publication_asset_to_dict(asset, settings),
    }


def auto_review_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id, "draft_id": row.draft_id, "status": row.status,
        "rule_report": normalized_review_report(row.rule_report_json),
        "model_report": normalized_review_report(row.model_report_json),
        "wechat_job_id": row.wechat_job_id, "error_message": row.error_message,
        "created_at": row.created_at, "finished_at": row.finished_at,
    }


def draft_revision_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "draft_id": row.draft_id,
        "auto_review_run_id": row.auto_review_run_id,
        "version": row.version,
        "summary_cn": row.summary_cn,
        "body": row.body,
        "tags": row.tags_json,
        "revision_reason": normalized_review_report(row.revision_reason_json),
        "created_at": row.created_at,
    }


def wechat_publication_to_dict(row: WechatPublicationJobRow, repository: ContentRepository) -> dict[str, Any]:
    draft = repository.get_draft(row.draft_id)
    return {
        "id": row.id,
        "draft_id": row.draft_id,
        "draft_title": (draft.title_options_json or ["未命名草稿"])[0],
        "draft_status": draft.status,
        "state": row.state,
        "cover_attachment_id": row.cover_attachment_id,
        "cover_asset_id": row.cover_asset_id,
        "inline_attachment_ids": row.inline_attachment_ids_json or [],
        "inline_asset_ids": row.inline_asset_ids_json or [],
        "inline_image_urls": row.inline_image_urls_json or [],
        "wechat_draft_media_id": row.wechat_draft_media_id,
        "wechat_publish_id": row.wechat_publish_id,
        "published_url": row.published_url,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def notification_to_dict(row: NotificationRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "category": row.category,
        "severity": row.severity,
        "target_view": row.target_view,
        "title": row.title,
        "detail": row.detail,
        "is_read": row.is_read,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def collection_run_to_dict(row: CollectionRunRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_run_id": row.agent_run_id,
        "source_kind": row.source_kind,
        "status": row.status,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "item_count": row.item_count,
        "created_count": row.created_count,
        "duplicate_count": row.duplicate_count,
        "skipped_count": row.skipped_count,
        "error_message": row.error_message,
    }


def trending_snapshot_to_dict(row: TrendingSnapshotRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "collection_run_id": row.collection_run_id,
        "source_item_id": row.source_item_id,
        "period": row.period,
        "rank": row.rank,
        "stars_period": row.stars_period,
        "stars_total": row.stars_total,
        "captured_at": row.captured_at,
    }


def agent_run_to_dict(row: AgentRunRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "action": row.action,
        "requested_sources": row.requested_sources_json,
        "limit": row.limit,
        "status": row.status,
        "tool_results": row.tool_results_json,
        "error_message": row.error_message,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def chat_session_to_dict(row: ChatSessionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def chat_message_to_dict(row: ChatMessageRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content,
        "created_at": row.created_at,
    }


def chat_agent_run_to_dict(
    row: ChatAgentRunRow, repository: ContentRepository
) -> dict[str, Any]:
    events = repository.list_chat_agent_events(row.id)
    def phase_progress(phase: str, default_label: str) -> dict[str, str]:
        matched = [event for event in events if event.metadata_json.get("phase") == phase]
        if not matched:
            enabled = row.auto_illustration_requested if phase == "image" else row.auto_review_requested
            return {
                "state": "pending" if enabled else "disabled",
                "label": default_label if enabled else f"未启用自动{'配图' if phase == 'image' else '审核'}",
            }
        latest = matched[-1]
        state = str(latest.metadata_json.get("state", latest.status))
        if phase == "review" and latest.title == "自动审核与草稿投递":
            results = latest.metadata_json.get("results", [])
            if isinstance(results, list) and results:
                statuses = {str(item.get("status")) for item in results if isinstance(item, dict)}
                if statuses & {"failed", "revision_failed"}:
                    return {"state": "rejected", "label": "自动审核未通过"}
                if statuses:
                    return {"state": "approved", "label": "自动审核通过"}
        if phase == "image" and latest.title == "自动配图规划":
            results = latest.metadata_json.get("results", [])
            if isinstance(results, list) and results and all(
                isinstance(item, dict) and not item.get("placements") for item in results
            ):
                return {"state": "skipped", "label": "未生成插图（无合适位置）"}
        return {"state": state, "label": latest.title}

    image_jobs = repository.list_image_generation_jobs(row.id)
    # 事件元数据里的 draft_ids 是运行与草稿的唯一关联；前端据此在草稿条目上显示“审核中/重写中”。
    draft_ids: list[str] = []
    for event in events:
        values = event.metadata_json.get("draft_ids", [])
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value not in draft_ids:
                draft_ids.append(value)
    return {
        "id": row.id,
        "request_message_id": row.request_message_id,
        "response_message_id": row.response_message_id,
        "intent": row.intent,
        "auto_review_requested": row.auto_review_requested,
        "auto_illustration_requested": row.auto_illustration_requested,
        "status": row.status,
        "summary": row.summary,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "attempt_started_at": row.attempt_started_at,
        "finished_at": row.finished_at,
        "tool_results": row.tool_results_json,
        "image_jobs": [image_generation_job_to_dict(job) for job in image_jobs],
        "draft_ids": draft_ids[:20],
        "progress": {
            "text": phase_progress("text", "等待文字生成"),
            "image": phase_progress("image", "等待配图规划"),
            "review": phase_progress("review", "等待自动审核"),
        },
        "events": [
            {
                "id": event.id,
                "status": event.status,
                "title": event.title,
                "detail": event.detail,
                "metadata": event.metadata_json,
                "created_at": event.created_at,
            }
            for event in events
        ],
    }


def image_generation_job_to_dict(row: ImageGenerationJobRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "chat_agent_run_id": row.chat_agent_run_id,
        "draft_id": row.draft_id,
        "purpose": row.purpose,
        "placement_after_paragraph": row.placement_after_paragraph,
        "status": row.status,
        "arq_job_id": row.arq_job_id,
        "illustration_id": row.illustration_id,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def schedule_plan_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "schedule_text": row.schedule_text,
        "task_summary": row.task_summary,
        "sources": row.sources_json,
        "confirmed_at": row.confirmed_at,
    }


def publish_plan_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "platform": row.platform,
        "draft_id": row.draft_id,
        "request_summary": row.request_summary,
        "confirmed_at": row.confirmed_at,
    }


def attachment_to_dict(row: AttachmentRow, settings: Settings) -> dict[str, Any]:
    token = AttachmentLinkSigner(settings).issue(row.id)
    return {
        "id": row.id,
        "session_id": row.session_id,
        "message_id": row.message_id,
        "original_name": row.original_name,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "status": row.status,
        "created_at": row.created_at,
        "download_url": (
            f"{settings.app_base_url}/api/attachments/{row.id}/download?token={token}"
        ),
    }


async def _bind_explicit_image_attachment(
    settings: Settings,
    repository: ContentRepository,
    session_id: str,
    attachment: AttachmentRow,
    draft_id: str,
    purpose: str,
    placement_after_paragraph: int,
) -> tuple[str, str]:
    """将明确引用的聊天图片复制为草稿独立素材，避免与聊天附件共享删除生命周期。"""
    if attachment.content_type not in {"image/jpeg", "image/png"}:
        raise HTTPException(status_code=400, detail="当前附件不是可用图片，请选择 JPG、JPEG 或 PNG 图片")
    store = PrivateAttachmentStore(settings)
    workspace = AgentWorkspaceStore(settings)
    try:
        try:
            content = workspace.read(session_id, attachment.id, attachment.original_name)
        except AttachmentAccessError:
            # 仅用户已明确要求将本图片用于草稿时，才允许为历史附件补建工作区副本。
            content = await run_in_threadpool(store.read, attachment.object_key)
            await run_in_threadpool(workspace.write, session_id, attachment.id, attachment.original_name, content)
        validate_image_attachment(attachment.original_name, content, attachment.content_type, settings)
        object_key, content_hash = await run_in_threadpool(
            store.upload, attachment.original_name, content, attachment.content_type
        )
    except AttachmentError as exc:
        raise HTTPException(status_code=503, detail="图片附件暂时不可读取或保存") from exc
    asset = repository.create_publication_asset(
        attachment.original_name,
        attachment.content_type,
        object_key,
        len(content),
        content_hash,
    )
    illustration = repository.create_draft_illustration(
        draft_id,
        asset.id,
        purpose,
        placement_after_paragraph,
        prompt="用户明确引用聊天附件作为草稿图片",
        provider="user_upload",
        model=None,
    )
    return illustration.id, asset.id


def _current_editable_draft(repository: ContentRepository, session_id: str) -> DraftRow | None:
    """仅使用当前会话明确记忆的草稿，禁止把全局最新草稿误当成用户目标。"""
    memory = repository.get_chat_session_memory(session_id)
    if not memory.active_draft_id:
        return None
    try:
        draft = repository.get_draft(memory.active_draft_id)
    except DraftNotFound:
        repository.update_chat_session_memory(session_id, clear_draft=True)
        return None
    if draft.status not in {"pending_review", "needs_revision", "ready_to_publish"}:
        return None
    return draft


def publication_asset_to_dict(row: PublicationAssetRow, settings: Settings) -> dict[str, Any]:
    token = AttachmentLinkSigner(settings).issue(row.id)
    return {
        "id": row.id,
        "original_name": row.original_name,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "created_at": row.created_at,
        "download_url": (
            f"{settings.app_base_url}/api/wechat/assets/{row.id}/download?token={token}"
        ),
    }


async def run_collection_agent(
    command: AgentCollectCommand, settings: Settings
) -> dict[str, Any]:
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        agent = ContentMainAgent(
            build_source_tools(client, settings), build_generator(settings)
        )
        return (await agent.run(command)).model_dump(mode="json")


@router.get("/health")
def health(session: SessionDependency) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}


@router.post("/collections/{source_name}")
async def collect_source(
    source_name: str,
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=25, ge=1, le=50),
) -> dict[str, Any]:
    if source_name == "all":
        sources: list[SourceKind] = []
    else:
        try:
            sources = [SourceKind(source_name)]
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail="未知来源，可选：all、arxiv、github、hacker_news、rss",
            ) from exc
    if sources == [SourceKind.RSS] and not settings.rss_feed_list:
        raise HTTPException(status_code=400, detail="请先通过 RSS_FEEDS 配置官方 RSS")
    try:
        return await run_collection_agent(
            AgentCollectCommand(
                sources=sources, limit=min(limit, settings.collect_limit)
            ),
            settings,
        )
    except AgentCommandError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/agent/collect")
async def collect_with_main_agent(
    command: AgentCollectCommand,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        command.limit = min(command.limit, settings.collect_limit)
        return await run_collection_agent(command, settings)
    except AgentCommandError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/agent/runs")
def list_agent_runs(
    session: SessionDependency,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return [
        agent_run_to_dict(row)
        for row in ContentRepository(session).list_agent_runs(limit)
    ]


@router.get("/collections/runs")
def list_collection_runs(
    session: SessionDependency,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    rows = ContentRepository(session).list_collection_runs(limit)
    return [collection_run_to_dict(row) for row in rows]


@router.get("/sources/health")
def source_health(
    session: SessionDependency,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    rows = ContentRepository(session).list_collection_runs(500)
    source_names = ("arxiv", "github", "hacker_news", "rss")
    health = []
    for source_kind in source_names:
        source_runs = [row for row in rows if row.source_kind == source_kind]
        latest = source_runs[0] if source_runs else None
        latest_success = next(
            (row for row in source_runs if row.status in {"success", "partial"}),
            None,
        )
        last_success_at = latest_success.finished_at if latest_success else None
        age_minutes = (
            (now - last_success_at).total_seconds() / 60
            if last_success_at is not None
            else None
        )
        health.append(
            {
                "source_kind": source_kind,
                "last_status": latest.status if latest else "never_run",
                "last_started_at": latest.started_at if latest else None,
                "last_success_at": last_success_at,
                "is_stale": age_minutes is None
                or age_minutes > settings.source_stale_after_minutes,
                "error_message": latest.error_message if latest else None,
            }
        )
    return health


@router.get("/sources")
def list_sources(
    session: SessionDependency,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return [source_to_dict(row) for row in ContentRepository(session).list_sources(limit)]


@router.get("/sources/{source_item_id}/trends")
def list_source_trends(
    source_item_id: str,
    session: SessionDependency,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    source = session.get(SourceItemRow, source_item_id)
    if source is None:
        raise HTTPException(status_code=404, detail="资讯不存在")
    if source.source_kind != "github":
        raise HTTPException(status_code=400, detail="仅 GitHub Trending 资讯提供趋势快照")
    rows = ContentRepository(session).list_trending_snapshots(source_item_id, limit)
    return [trending_snapshot_to_dict(row) for row in rows]


@router.post("/chat/sessions")
def create_chat_session(
    command: ChatSessionCreate, session: SessionDependency
) -> dict[str, Any]:
    row = ContentRepository(session).create_chat_session(command.title)
    session.commit()
    return chat_session_to_dict(row)


@router.get("/chat/sessions")
def list_chat_sessions(
    session: SessionDependency, limit: int = Query(default=100, ge=1, le=500)
) -> list[dict[str, Any]]:
    return [
        chat_session_to_dict(row)
        for row in ContentRepository(session).list_chat_sessions(limit)
    ]


@router.delete("/chat/sessions/{session_id}")
def delete_chat_session(
    session_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, str]:
    ContentRepository(session).delete_chat_session(session_id)
    session.commit()
    try:
        AgentWorkspaceStore(settings).delete_session(session_id)
    except OSError:
        # 数据库删除已成功；遗留副本不应导致前端误报删除失败，后续可按会话 ID 清理。
        logger.warning("chat_workspace_cleanup_failed session_id=%s", session_id)
    return {"deleted_id": session_id}


@router.get("/chat/sessions/{session_id}")
def get_chat_session(
    session_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    repository = ContentRepository(session)
    chat_session = repository.get_chat_session(session_id)
    return {
        "session": chat_session_to_dict(chat_session),
        "messages": [
            chat_message_to_dict(row) for row in repository.list_chat_messages(session_id)
        ],
        "attachments": [
            attachment_to_dict(row, settings) for row in repository.list_attachments(session_id)
        ],
        "agent_runs": [
            chat_agent_run_to_dict(row, repository)
            for row in repository.list_chat_agent_runs(session_id)
        ],
    }


def reconcile_stale_generation_runs(
    settings: Settings, repository: ContentRepository
) -> int:
    """页面刷新时收束确定已失联的生成任务，避免永久显示“正在生成”。"""
    rows = repository.reconcile_stale_generation_runs(
        settings.generation_run_stale_timeout_seconds
    )
    if rows:
        logger.warning("stale_generation_runs_reconciled count=%s", len(rows))
    return len(rows)


@router.get("/chat/agent-runs/active")
def list_active_chat_agent_runs(
    settings: Annotated[Settings, Depends(get_settings)], session: SessionDependency
) -> list[dict[str, Any]]:
    repository = ContentRepository(session)
    if reconcile_stale_generation_runs(settings, repository):
        session.commit()
    rows = repository.list_active_chat_agent_runs()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = chat_agent_run_to_dict(row, repository)
        request_message = session.get(ChatMessageRow, row.request_message_id)
        chat_session = session.get(ChatSessionRow, row.session_id)
        payload["request_text"] = request_message.content if request_message else "生成任务"
        payload["session_title"] = chat_session.title if chat_session else "已删除会话"
        result.append(payload)
    return result


@router.get("/notifications")
def list_notifications(
    session: SessionDependency,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """统一返回失败通知；首次读取时补齐既有失败审计，不读取任何外部服务。"""
    repository = ContentRepository(session)
    repository.sync_failure_notifications()
    session.commit()
    return {
        "items": [notification_to_dict(row) for row in repository.list_notifications(limit=limit)],
        "unread_count": repository.unread_notification_count(),
    }


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, session: SessionDependency) -> dict[str, Any]:
    try:
        row = ContentRepository(session).mark_notification_read(notification_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="通知不存在或已删除") from exc
    session.commit()
    return notification_to_dict(row)


@router.post("/notifications/read-all")
def mark_all_notifications_read(session: SessionDependency) -> dict[str, int]:
    count = ContentRepository(session).mark_all_notifications_read()
    session.commit()
    return {"updated_count": count}


@router.delete("/notifications/{notification_id}")
def dismiss_notification(notification_id: str, session: SessionDependency) -> dict[str, str]:
    try:
        ContentRepository(session).dismiss_notification(notification_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="通知不存在或已删除") from exc
    session.commit()
    return {"deleted_id": notification_id}


@router.get("/chat/agent-runs/generation-records")
def list_generation_chat_agent_runs(
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
    limit: int = Query(default=100, ge=1, le=300),
) -> list[dict[str, Any]]:
    """生成记录统一展示运行中、完成、失败和超时任务。"""
    repository = ContentRepository(session)
    if reconcile_stale_generation_runs(settings, repository):
        session.commit()
    result: list[dict[str, Any]] = []
    for row in repository.list_generation_chat_agent_runs(limit):
        payload = chat_agent_run_to_dict(row, repository)
        request_message = session.get(ChatMessageRow, row.request_message_id)
        chat_session = session.get(ChatSessionRow, row.session_id)
        payload["request_text"] = request_message.content if request_message else "生成任务"
        payload["session_title"] = chat_session.title if chat_session else "已删除会话"
        result.append(payload)
    return result


@router.delete("/chat/agent-runs/generation-records/{run_id}")
def delete_generation_chat_agent_run(
    run_id: str, session: SessionDependency
) -> dict[str, str]:
    """删除终态生成任务审计及其关联通知，不删除草稿、插图或聊天会话。"""
    try:
        deleted_id = ContentRepository(session).delete_generation_run_audit(run_id)
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"deleted_id": deleted_id}


def _retry_draft_id(repository: ContentRepository, run: ChatAgentRunRow) -> str | None:
    """从原运行的阶段审计恢复草稿；无草稿时由原来源范围重新采集。"""
    for event in reversed(repository.list_chat_agent_events(run.id)):
        draft_ids = event.metadata_json.get("draft_ids", [])
        if not isinstance(draft_ids, list):
            continue
        for draft_id in draft_ids:
            if not isinstance(draft_id, str):
                continue
            try:
                repository.get_draft(draft_id)
            except DraftNotFound:
                continue
            return draft_id
    return None


def _retry_collection_options(repository: ContentRepository, run: ChatAgentRunRow) -> tuple[list[str], int]:
    """仅复用原采集审计中保存的来源和数量，拒绝猜测来源。"""
    for event in reversed(repository.list_chat_agent_events(run.id)):
        sources = event.metadata_json.get("sources")
        if not isinstance(sources, list) or not sources:
            continue
        safe_sources = [source for source in sources if source in {kind.value for kind in SourceKind}]
        if not safe_sources:
            continue
        try:
            limit = max(1, min(int(event.metadata_json.get("limit", 5)), 50))
        except (TypeError, ValueError):
            limit = 5
        return safe_sources, limit
    raise RepositoryError("原任务缺少可重试的来源范围，请重新发起采集")


@router.post("/chat/agent-runs/generation-records/{run_id}/retry")
async def retry_generation_chat_agent_run(
    run_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    """在原生成记录内重试，优先更新已有草稿，不创建第二条运行审计。"""
    repository = ContentRepository(session)
    try:
        run = repository.get_chat_agent_run(run_id)
        if run.intent != ConversationIntent.COLLECT_NEWS.value:
            raise RepositoryError("仅能重试生成队列中的任务")
        if run.status != ConversationRunStatus.FAILED.value:
            raise RepositoryError("仅生成失败的任务可重试")
        draft_id = _retry_draft_id(repository, run)
        if draft_id:
            reply = "已在原生成记录内重新生成，将保留原草稿和历史版本。"
        else:
            source_names, limit = _retry_collection_options(repository, run)
            reply = "已按原来源范围在原生成记录内重新发起采集和文案生成。"
        assistant_message = repository.create_chat_message(run.session_id, "assistant", reply)
        repository.reopen_generation_run(
            run, assistant_message.id,
            run.auto_review_requested, run.auto_illustration_requested,
        )
        if draft_id:
            repository.add_chat_agent_event(
                run.id, "原记录重试已入队", "将基于已保存来源重新生成，原草稿不会被提前覆盖。", "running",
                metadata={"phase": "text", "state": "running", "draft_ids": [draft_id], "regeneration": True, "retry": True},
            )
        else:
            repository.add_chat_agent_event(
                run.id, "原记录重试已入队", "将按原来源范围重新采集并生成文案。", "running",
                metadata={"phase": "text", "state": "running", "sources": source_names, "limit": limit, "retry": True},
            )
        session.commit()
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        if draft_id:
            job_id = await enqueue_draft_regeneration_job(
                settings, run.id, run.session_id, assistant_message.id, draft_id,
                run.auto_review_requested, run.auto_illustration_requested,
            )
        else:
            job_id = await enqueue_collection_job(
                settings, run.id, run.session_id, assistant_message.id, source_names, limit,
                run.auto_review_requested, run.auto_illustration_requested,
            )
        repository.add_chat_agent_event(
            run.id, "重试后台任务已创建", f"任务编号：{job_id}",
            metadata={"phase": "text", "state": "running", "job_id": job_id, "retry": True, **({"draft_ids": [draft_id]} if draft_id else {"sources": source_names, "limit": limit})},
        )
        session.commit()
    except Exception as exc:
        logger.exception("generation_retry_enqueue_failed run_id=%s error_type=%s", run.id, type(exc).__name__)
        detail = "重试任务未能入队，原草稿和历史记录已保留。"
        repository.update_chat_message(assistant_message.id, detail)
        repository.finish_chat_agent_run(run.id, assistant_message.id, ConversationRunStatus.FAILED, "原记录重试入队失败", [], detail)
        repository.upsert_failure_notification("chat_agent_run", run.id, "generation", "文案重试失败", detail, "review")
        session.commit()
        raise HTTPException(status_code=503, detail=detail) from exc
    return {"message": chat_message_to_dict(assistant_message), "execution": chat_agent_run_to_dict(run, repository)}


@router.get("/image-generation-jobs/{job_id}")
def get_image_generation_job(job_id: str, session: SessionDependency) -> dict[str, Any]:
    """按公开图片任务 ID 查询状态；不返回供应商密钥或原始响应。"""
    try:
        row = ContentRepository(session).get_image_generation_job(job_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="图片生成任务不存在") from exc
    return image_generation_job_to_dict(row)


@router.post("/chat/sessions/{session_id}/attachments")
async def upload_chat_attachment(
    session_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    repository = ContentRepository(session)
    repository.get_chat_session(session_id)
    content = await file.read(settings.attachment_max_bytes + 1)
    try:
        object_key, content_hash = await run_in_threadpool(
            PrivateAttachmentStore(settings).upload,
            file.filename or "attachment.txt",
            content,
            file.content_type or "text/plain",
        )
    except AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    message = repository.create_chat_message(
        session_id, "user", f"上传附件：{file.filename or 'attachment.txt'}"
    )
    attachment = repository.create_attachment(
        session_id=session_id,
        message_id=message.id,
        original_name=file.filename or "attachment.txt",
        content_type=file.content_type or "text/plain",
        object_key=object_key,
        size_bytes=len(content),
        sha256=content_hash,
    )
    try:
        await run_in_threadpool(
            AgentWorkspaceStore(settings).write,
            session_id,
            attachment.id,
            attachment.original_name,
            content,
        )
    except OSError as exc:
        session.rollback()
        try:
            await run_in_threadpool(PrivateAttachmentStore(settings).delete, object_key)
        except AttachmentError:
            logger.warning("attachment_upload_workspace_cleanup_failed session_id=%s", session_id)
        raise HTTPException(status_code=503, detail="附件工作区暂时不可写入") from exc
    session.commit()
    logger.info("chat_attachment_stored session_id=%s attachment_id=%s", session_id, attachment.id)
    return attachment_to_dict(attachment, settings)


@router.post("/chat/sessions/{session_id}/messages")
async def send_chat_message(
    session_id: str,
    command: ChatMessageCreate,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    repository = ContentRepository(session)
    user_message = repository.create_chat_message(session_id, "user", command.content)
    repository.set_first_instruction_title(session_id, command.content)
    logger.info(
        "chat_message_accepted session_id=%s message_id=%s content_length=%s has_attachment=%s",
        session_id,
        user_message.id,
        len(command.content),
        command.attachment_id is not None,
    )
    if command.attachment_id:
        repository.update_chat_session_memory(session_id, active_attachment_id=command.attachment_id)
    command_text = command.content.strip()
    if command_text.startswith("运行脚本"):
        if not command.attachment_id:
            assistant_message = repository.create_chat_message(session_id, "assistant", "请先选择当前会话中的文本附件，并使用“运行脚本 <登记脚本ID>”。")
            session.commit()
            return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": None}
        script_id = command_text.removeprefix("运行脚本").strip().split(maxsplit=1)[0] if command_text.removeprefix("运行脚本").strip() else ""
        try:
            attachment = repository.get_attachment(command.attachment_id)
            workspace = SessionWorkspaceTool(
                PrivateAttachmentStore(settings), AgentWorkspaceStore(settings)
            )
            source_file = workspace.read_text(session_id, attachment)
            output = run_registered_script(script_id, source_file.content)
            object_key, content_hash, size_bytes = workspace.write_text(f"{script_id}-{source_file.filename}.md", output)
            generated = repository.create_attachment(session_id, user_message.id, f"{script_id}-{source_file.filename}.md", "text/markdown", object_key, size_bytes, content_hash)
            link = attachment_to_dict(generated, settings)["download_url"]
            assistant_message = repository.create_chat_message(session_id, "assistant", f"已运行登记脚本 `{script_id}`，未执行 Shell 或网络请求。下载结果：{link}")
        except (RegisteredScriptError, ValueError) as exc:
            assistant_message = repository.create_chat_message(session_id, "assistant", f"脚本未执行：{exc}")
        session.commit()
        return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": None}
    agent = ChatAgent()
    attachment_extraction_requested = agent.requests_attachment_extraction(command.content, command.attachment_id)
    decision = None
    resolution = None
    if not attachment_extraction_requested:
        # 先提交用户消息，前端可立即显示；后续由同一 session_id 的 DeepAgent 恢复 checkpoint 决策。
        session.commit()
        resolution = await ContentDeepAgent(settings).resolve(
            session_id, command.content, command.attachment_id is not None,
        )
        decision = resolution.decision
        logger.info(
            "chat_deep_agent_resolved session_id=%s intent=%s attachment=%s success=%s failure_kind=%s",
            session_id, decision.intent.value, command.attachment_id is not None,
            resolution.success, resolution.failure_kind,
        )
    if command.attachment_id and decision and decision.intent == ConversationIntent.GENERATE_DRAFT_IMAGE:
        attachment = repository.get_attachment(command.attachment_id)
        if attachment.session_id != session_id:
            raise HTTPException(status_code=400, detail="附件不属于当前对话")
        purpose, placement_after_paragraph = decision.image_purpose, decision.placement_after_paragraph
        run = repository.create_chat_agent_run(
            session_id, user_message.id, ConversationIntent.GENERATE_DRAFT_IMAGE,
            command.auto_review, command.auto_illustration,
        )
        repository.add_chat_agent_event(
            run.id,
            "识别附件图片用途",
            f"用户明确要求将当前图片用作{'封面' if purpose == 'cover' else f'正文第 {placement_after_paragraph} 段后插图'}。",
            metadata={"phase": "image", "state": "binding", "purpose": purpose},
        )
        target = _current_editable_draft(repository, session_id)
        if target is None:
            reply = "当前会话还没有已选择的可编辑草稿。请先生成草稿，或明确告诉我需要处理哪篇文章。"
            status = ConversationRunStatus.FAILED
            results: list[dict[str, Any]] = []
            repository.add_chat_agent_event(run.id, "图片绑定未执行", reply, "failed", metadata={"phase": "image", "state": "failed"})
        else:
            try:
                repository.add_chat_agent_event(
                    run.id,
                    "读取受控工作区图片",
                    "已按明确指令读取当前会话图片；未发送给无关服务。",
                    "running",
                    metadata={"phase": "image", "state": "reading"},
                )
                illustration_id, asset_id = await _bind_explicit_image_attachment(
                    settings, repository, session_id, attachment, target.id, purpose, placement_after_paragraph
                )
                reply = f"已将“{attachment.original_name}”设为“{(target.title_options_json or ['草稿'])[0]}”的{'封面' if purpose == 'cover' else f'第 {placement_after_paragraph} 段后插图'}，可在生成记录中调整或移除。"
                status = ConversationRunStatus.COMPLETED
                results = [{"tool": "bind_uploaded_image", "draft_id": target.id, "illustration_id": illustration_id, "asset_id": asset_id, "purpose": purpose, "placement_after_paragraph": placement_after_paragraph}]
                repository.add_chat_agent_event(
                    run.id,
                    "图片已绑定到草稿",
                    "图片已复制为草稿私有素材，未上传公众号。",
                    metadata={"phase": "image", "state": "completed", **results[0]},
                )
            except HTTPException as exc:
                reply = str(exc.detail)
                status = ConversationRunStatus.FAILED
                results = []
                repository.add_chat_agent_event(run.id, "图片绑定失败", reply, "failed", metadata={"phase": "image", "state": "failed"})
        assistant_message = repository.create_chat_message(session_id, "assistant", reply)
        repository.finish_chat_agent_run(
            run.id, assistant_message.id, status, reply, results,
            reply if status == ConversationRunStatus.FAILED else None,
        )
        session.commit()
        return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": chat_agent_run_to_dict(run, repository)}

    target_draft = _current_editable_draft(repository, session_id)
    if decision and decision.intent == ConversationIntent.REGENERATE_DRAFT and target_draft is not None:
        origin_run = repository.find_generation_run_for_draft(target_draft.id)
        if origin_run is None:
            reply = "当前草稿缺少可回溯的原生成记录，未创建新任务；请先在生成记录中选择原文案后重试。"
            assistant_message = repository.create_chat_message(session_id, "assistant", reply)
            session.commit()
            return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": None}
        assistant_message = repository.create_chat_message(
            session_id, "assistant", "已在原生成记录内发起重新获取，将更新原草稿版本，不会新建文章或生成记录。"
        )
        repository.reopen_generation_run(
            origin_run, assistant_message.id, command.auto_review,
            command.auto_illustration or decision.auto_illustration,
        )
        repository.add_chat_agent_event(
            origin_run.id,
            "重新获取项目内容",
            f"将更新原草稿版本 {target_draft.version}，保留原来源、图片和审核记录。",
            "running",
            metadata={"phase": "text", "state": "running", "draft_ids": [target_draft.id], "regeneration": True},
        )
        session.commit()
        try:
            job_id = await enqueue_draft_regeneration_job(
                settings, origin_run.id, session_id, assistant_message.id, target_draft.id,
                origin_run.auto_review_requested, origin_run.auto_illustration_requested,
            )
            repository.add_chat_agent_event(
                origin_run.id, "正在重新生成文案", f"任务编号：{job_id}；将用来源快照重建正文并覆盖为新版本。",
                metadata={"phase": "text", "state": "running", "job_id": job_id, "draft_ids": [target_draft.id], "regeneration": True},
            )
            session.commit()
        except Exception as exc:
            logger.exception("draft_regeneration_enqueue_failed run_id=%s error_type=%s", origin_run.id, type(exc).__name__)
            repository.update_chat_message(assistant_message.id, "原记录重生成任务未能入队，请稍后重试。")
            repository.finish_chat_agent_run(origin_run.id, assistant_message.id, ConversationRunStatus.FAILED, "原记录重生成入队失败", [], str(exc))
            session.commit()
        return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": chat_agent_run_to_dict(origin_run, repository)}

    target_draft = _current_editable_draft(repository, session_id)
    if decision and decision.intent == ConversationIntent.RUN_AUTO_REVIEW and target_draft is not None:
        # 先清理超时/被重启打断的僵死审核记录，否则它会一直挡住新的审核。
        repository.expire_stale_auto_review_run(target_draft.id, settings.collection_job_timeout_seconds)
        session.commit()
        existing_review = repository.find_active_auto_review_run(target_draft.id)
        if existing_review is not None:
            assistant_message = repository.create_chat_message(
                session_id, "assistant", "这篇的自动审核已经在处理中，完成后我会汇报结果。"
            )
            session.commit()
            return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": None}
        review_run = repository.create_auto_review_run(target_draft.id, None, status="queued")
        # 审核也要有一条可见的运行记录，否则用户在“生成记录”里看不到任何正在跑的任务。
        chat_run = repository.create_chat_agent_run(
            session_id, user_message.id, ConversationIntent.RUN_AUTO_REVIEW, False, False
        )
        repository.add_chat_agent_event(
            chat_run.id, "识别对话意图", "已识别为：立即审核当前文案（仅审核，不投递）",
            metadata={"intent": ConversationIntent.RUN_AUTO_REVIEW.value, "draft_ids": [target_draft.id]},
        )
        repository.add_chat_agent_event(
            chat_run.id, "自动审核已入队",
            f"正在审核《{(target_draft.title_options_json or ['当前草稿'])[0]}》并按其意见改稿一轮；通过后不会自动发表。",
            "running",
            metadata={"phase": "review", "state": "running", "draft_ids": [target_draft.id], "review_id": review_run.id},
        )
        assistant_message = repository.create_chat_message(
            session_id,
            "assistant",
            f"好，正在对《{(target_draft.title_options_json or ['当前草稿'])[0]}》运行自动审核；通过后不会自动发表。",
        )
        chat_run.response_message_id = assistant_message.id
        chat_run.summary = "自动审核中"
        chat_run.status = ConversationRunStatus.RUNNING.value
        session.commit()
        try:
            job_id = await enqueue_auto_review_job(
                settings, target_draft.id, review_run.id, False, chat_run.id
            )
        except Exception as exc:
            repository.finish_auto_review_run(review_run.id, "failed", {}, {}, "自动审核任务入队失败，请稍后重试")
            repository.update_chat_message(assistant_message.id, "自动审核任务未能入队，请稍后重试。")
            repository.finish_chat_agent_run(
                chat_run.id, assistant_message.id, ConversationRunStatus.FAILED, "自动审核入队失败", [], str(exc)
            )
            session.commit()
            logger.exception("chat_auto_review_enqueue_failed draft_id=%s error_type=%s", target_draft.id, type(exc).__name__)
            raise HTTPException(status_code=503, detail="自动审核任务未能入队，请稍后重试") from exc
        repository.add_chat_agent_event(
            chat_run.id, "审核任务已创建", f"任务编号：{job_id}",
            metadata={"phase": "review", "state": "running", "job_id": job_id, "draft_ids": [target_draft.id]},
        )
        logger.info("chat_auto_review_enqueued draft_id=%s review_id=%s job_id=%s", target_draft.id, review_run.id, job_id)
        session.commit()
        return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": chat_agent_run_to_dict(chat_run, repository)}

    if decision and decision.intent == ConversationIntent.REUSE_DRAFT_ASSETS and target_draft is not None:
        illustrations = repository.list_draft_illustrations(target_draft.id)
        if not illustrations:
            assistant_message = repository.create_chat_message(
                session_id, "assistant", "这篇目前没有可复用的配图；需要的话我可以按当前正文重新生成配图。"
            )
            session.commit()
            return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": None}
        # 复用＝用当前草稿已有的插图固化一次投递选择：封面取现有封面（没有就用第一张），正文取其余全部。
        cover = next((item for item in illustrations if item.purpose == "cover"), illustrations[0])
        inline = [item for item in illustrations if item.id != cover.id]
        repository.save_wechat_asset_selection(target_draft.id, cover.asset_id, [item.asset_id for item in inline])
        assistant_message = repository.create_chat_message(
            session_id,
            "assistant",
            f"已复用当前草稿的配图：封面 1 张、正文插图 {len(inline)} 张；正文与图片都不变，可直接进入发布流程。",
        )
        session.commit()
        logger.info("chat_reuse_draft_assets draft_id=%s inline=%s", target_draft.id, len(inline))
        return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": None}

    if not attachment_extraction_requested:
        assert decision is not None
        if decision.intent == ConversationIntent.COLLECT_NEWS:
            target = normalize_github_target(decision.target)
            source_names = [source.value for source in decision.sources]
            if target and SourceKind.GITHUB.value not in source_names:
                # 点名了项目就必须包含 GitHub 来源，避免模型漏给 sources 时又去跑别的榜单。
                source_names.insert(0, SourceKind.GITHUB.value)
            auto_illustration_requested = command.auto_illustration or decision.auto_illustration
            run = repository.create_chat_agent_run(
                session_id, user_message.id, decision.intent,
                command.auto_review, auto_illustration_requested,
            )
            repository.add_chat_agent_event(
                run.id, "识别对话意图",
                f"已识别为：按指定项目生成（{target}）" if target else "已识别为：采集资讯",
                metadata={"intent": decision.intent.value, "target": target},
            )
            repository.add_chat_agent_event(
                run.id, "加入后台采集队列",
                f"任务已入队；将只抓取指定项目 {target} 并生成待审核草稿，不读取榜单。"
                if target
                else "任务已入队；将按已登记来源规则生成待审核草稿。",
                metadata={
                    "sources": source_names,
                    "limit": decision.limit,
                    "auto_illustration": auto_illustration_requested,
                    "target": target,
                },
            )
            repository.add_chat_agent_event(
                run.id,
                "文字生成中",
                f"正在获取 {target} 的仓库信息与 README，并生成待审核文案。"
                if target
                else "正在采集来源、提取证据并生成待审核文案。",
                "running",
                metadata={"phase": "text", "state": "running", "target": target},
            )
            assistant_message = repository.create_chat_message(
                session_id,
                "assistant",
                f"已加入后台队列，正在按指定项目 {target} 获取资料并生成待审核草稿。"
                if target
                else "已加入后台队列，正在采集并生成待审核草稿。",
            )
            run.response_message_id = assistant_message.id
            run.summary = "后台任务已排队"
            run.status = ConversationRunStatus.RUNNING.value
            session.commit()
            try:
                job_id = await enqueue_collection_job(
                    settings, run.id, session_id, assistant_message.id, source_names, decision.limit,
                    command.auto_review, auto_illustration_requested, target,
                )
                repository.add_chat_agent_event(
                    run.id, "后台任务已创建", f"任务编号：{job_id}", metadata={"job_id": job_id}
                )
                session.commit()
                logger.info(
                    "chat_collection_enqueued run_id=%s sources=%s limit=%s job_id=%s",
                    run.id,
                    source_names,
                    decision.limit,
                    job_id,
                )
            except Exception as exc:
                logger.exception(
                    "chat_collection_enqueue_failed run_id=%s error_type=%s",
                    run.id,
                    type(exc).__name__,
                )
                repository.update_chat_message(assistant_message.id, "后台队列不可用，请稍后重试。")
                repository.finish_chat_agent_run(
                    run.id, assistant_message.id, ConversationRunStatus.FAILED,
                    "后台任务创建失败", [], str(exc),
                )
                session.commit()
            return {
                "message": chat_message_to_dict(assistant_message),
                "processing": None,
                "execution": chat_agent_run_to_dict(run, repository),
            }

        if decision.intent == ConversationIntent.GENERATE_DRAFT_IMAGE:
            run = repository.create_chat_agent_run(
                session_id, user_message.id, decision.intent,
                command.auto_review, command.auto_illustration,
            )
            repository.add_chat_agent_event(run.id, "识别对话意图", "已识别为：生成草稿插图")
            target = _current_editable_draft(repository, session_id)
            if target is None:
                reply = "当前会话还没有已选择的可编辑草稿。请先生成草稿，或明确指定需要配图的文章。"
                status = ConversationRunStatus.FAILED
                results: list[dict[str, Any]] = []
            else:
                purpose = decision.image_purpose
                try:
                    result = await ImageGenerationTool(
                        settings, repository
                    ).invoke(target.id, purpose, decision.placement_after_paragraph)
                    reply = f"已为“{(target.title_options_json or ['草稿'])[0]}”生成私有{('封面' if purpose == 'cover' else '插图')}，可在生成记录中调整位置或移除。"
                    status = ConversationRunStatus.COMPLETED
                    results = [{"tool": "generate_draft_image", "draft_id": target.id, "illustration_id": result.illustration_id, "asset_id": result.asset_id, "purpose": purpose}]
                    repository.add_chat_agent_event(run.id, "生成草稿插图 Tool", "图片已私有保存，未上传公众号。", metadata=results[0])
                except ImageGenerationError as exc:
                    reply = f"图片未生成：{exc}"
                    status = ConversationRunStatus.FAILED
                    results = []
                    repository.add_chat_agent_event(run.id, "图片生成未执行", str(exc), "failed")
            assistant_message = repository.create_chat_message(session_id, "assistant", reply)
            repository.finish_chat_agent_run(run.id, assistant_message.id, status, reply, results, reply if status == ConversationRunStatus.FAILED else None)
            session.commit()
            return {"message": chat_message_to_dict(assistant_message), "processing": None, "execution": chat_agent_run_to_dict(run, repository)}

        run = repository.create_chat_agent_run(
            session_id, user_message.id, ConversationIntent.GENERAL_CHAT,
            command.auto_review, command.auto_illustration,
        )
        assert resolution is not None
        if resolution.success:
            summary = "已完成会话回复"
            repository.add_chat_agent_event(
                run.id,
                "DeepAgent 会话决策已生成",
                "已基于同一会话的持久记忆和受控上下文生成回复。",
                metadata={"phase": "chat", "state": "completed", "intent": decision.intent.value},
            )
            status = ConversationRunStatus.COMPLETED
            error_message = None
        else:
            summary = decision.reply
            repository.add_chat_agent_event(
                run.id,
                "DeepAgent 会话决策未完成",
                "未执行采集、配图、改稿或发布操作；失败类别已安全记录。",
                "failed",
                metadata={"phase": "chat", "state": "failed", "failure_kind": resolution.failure_kind},
            )
            status = ConversationRunStatus.FAILED
            error_message = summary
        assistant_message = repository.create_chat_message(session_id, "assistant", decision.reply)
        repository.finish_chat_agent_run(
            run.id, assistant_message.id, status, summary, [], error_message,
        )
        session.commit()
        logger.info(
            "chat_deep_agent_reply_finished run_id=%s session_id=%s status=%s",
            run.id, session_id, status.value,
        )
        return {
            # 前端以该消息替换本地乐观用户消息；随后 reload 会同时带回助手回复。
            "message": chat_message_to_dict(user_message),
            "processing": None,
            "execution": chat_agent_run_to_dict(run, repository),
        }

    attachment = repository.get_attachment(command.attachment_id or "")
    if attachment.session_id != session_id:
        raise HTTPException(status_code=400, detail="附件不属于当前对话")
    if not settings.llm_enabled:
        assistant_message = repository.create_chat_message(
            session_id,
            "assistant",
            "已识别明确提取指令，但 LLM_ENABLED=false，附件没有被读取或处理。",
        )
        session.commit()
        return {
            "message": chat_message_to_dict(assistant_message),
            "processing": None,
            "execution": None,
        }

    processing = repository.create_attachment_processing(attachment.id, user_message.id)
    session.commit()
    try:
        content = SessionWorkspaceTool(
            PrivateAttachmentStore(settings), AgentWorkspaceStore(settings)
        ).read_text(session_id, attachment).content[:30000]
        download_url = attachment_to_dict(attachment, settings)["download_url"]
        raw_item = RawSourceItem(
            source_kind=SourceKind.ATTACHMENT,
            external_id=attachment.id,
            title=attachment.original_name,
            url=download_url,
            summary=content[:5000],
            content=content,
            source_name=f"对话附件：{attachment.original_name}",
            metadata={"attachment_id": attachment.id},
        )
        result = ContentPipeline(session, build_generator(settings), settings).process([raw_item])
        source = repository.get_source_by_external_id(
            SourceKind.ATTACHMENT.value, attachment.id
        )
        draft = repository.get_draft_for_source(source.id) if source else None
        if result["errors"] or draft is None:
            error_message = "; ".join(
                error["error"] for error in result["errors"]
            ) or "未生成待审核草稿"
            repository.finish_attachment_processing(
                processing.id, AttachmentStatus.FAILED, error_message=error_message
            )
            assistant_content = f"附件提取失败：{error_message}"
            draft_id = None
        else:
            repository.finish_attachment_processing(
                processing.id, AttachmentStatus.PROCESSED, draft_id=draft.id
            )
            assistant_content = f"已生成待审核草稿：{draft.id}"
            draft_id = draft.id
        assistant_message = repository.create_chat_message(
            session_id, "assistant", assistant_content
        )
        session.commit()
        return {
            "message": chat_message_to_dict(assistant_message),
            "processing": {"id": processing.id, "draft_id": draft_id},
            "execution": None,
        }
    except Exception as exc:
        session.rollback()
        repository.finish_attachment_processing(
            processing.id, AttachmentStatus.FAILED, error_message=str(exc)
        )
        assistant_message = repository.create_chat_message(
            session_id, "assistant", f"附件提取失败：{exc}"
        )
        session.commit()
        return {
            "message": chat_message_to_dict(assistant_message),
            "processing": {"id": processing.id, "draft_id": None},
            "execution": None,
        }


@router.post("/schedule-plans/{plan_id}/confirm")
def confirm_schedule_plan(
    plan_id: str,
    command: PlanConfirmationCommand,
    session: SessionDependency,
) -> dict[str, Any]:
    row = ConfirmSchedulePlanTool().invoke(
        ContentRepository(session), ConfirmPlanRequest(plan_id, command.idempotency_key)
    )
    session.commit()
    return schedule_plan_to_dict(row)


@router.post("/publish-plans/{plan_id}/confirm")
def confirm_publish_plan(
    plan_id: str,
    command: PlanConfirmationCommand,
    session: SessionDependency,
) -> dict[str, Any]:
    row = ConfirmPublishPlanTool().invoke(
        ContentRepository(session), ConfirmPlanRequest(plan_id, command.idempotency_key)
    )
    session.commit()
    return publish_plan_to_dict(row)


@router.get("/attachments/{attachment_id}/download")
def download_attachment(
    attachment_id: str,
    token: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> Response:
    try:
        AttachmentLinkSigner(settings).verify(attachment_id, token)
        attachment = ContentRepository(session).get_attachment(attachment_id)
        content = PrivateAttachmentStore(settings).read(attachment.object_key)
    except AttachmentAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AttachmentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AttachmentError as exc:
        raise HTTPException(status_code=503, detail="附件暂时不可下载") from exc
    return Response(
        content=content,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": (
                "attachment; "
                f"filename*=UTF-8''{quote(attachment.original_name)}"
            )
        },
    )


@router.get("/wechat/assets/{asset_id}/download")
def download_publication_asset(
    asset_id: str,
    token: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> Response:
    try:
        AttachmentLinkSigner(settings).verify(asset_id, token)
        asset = ContentRepository(session).get_publication_asset(asset_id)
        content = PrivateAttachmentStore(settings).read(asset.object_key)
    except AttachmentAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PublicationAssetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AttachmentError as exc:
        raise HTTPException(status_code=503, detail="发布素材暂时不可下载") from exc
    return Response(
        content=content,
        media_type=asset.content_type,
        headers={
            "Content-Disposition": (
                "inline; " f"filename*=UTF-8''{quote(asset.original_name)}"
            )
        },
    )


@router.get("/drafts")
def list_drafts(
    session: SessionDependency,
    settings: Annotated[Settings, Depends(get_settings)],
    status: str | None = None,
    created_from: date | None = Query(default=None, description="按上海时区的草稿创建日期筛选起始日"),
    created_to: date | None = Query(default=None, description="按上海时区的草稿创建日期筛选结束日"),
    include_deleted: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    if created_from and created_to and created_from > created_to:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    shanghai = ZoneInfo("Asia/Shanghai")
    created_from_at = (
        datetime.combine(created_from, time.min, tzinfo=shanghai).astimezone(UTC)
        if created_from
        else None
    )
    created_until = (
        datetime.combine(created_to + timedelta(days=1), time.min, tzinfo=shanghai).astimezone(UTC)
        if created_to
        else None
    )
    return [
        draft_to_dict(row, settings)
        for row in ContentRepository(session).list_drafts(
            status=status,
            limit=limit,
            created_from=created_from_at,
            created_until=created_until,
            include_deleted=include_deleted,
        )
    ]


@router.get("/drafts/{draft_id}")
def get_draft(
    draft_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    return draft_to_dict(ContentRepository(session).get_draft(draft_id), settings)


@router.delete("/drafts/{draft_id}")
def delete_draft(
    draft_id: str,
    session: SessionDependency,
) -> dict[str, str]:
    """软删除未发布、未产生公众号投递记录的草稿；不触碰远端公众号数据。"""
    ContentRepository(session).delete_draft(draft_id)
    session.commit()
    logger.info("审核草稿已从默认列表删除 draft_id=%s", draft_id)
    return {"deleted_id": draft_id}


@router.patch("/drafts/{draft_id}")
def edit_draft(
    draft_id: str,
    patch: DraftEdit,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    row = ContentRepository(session).edit_draft(draft_id, patch)
    session.commit()
    return draft_to_dict(row, settings)


@router.post("/drafts/{draft_id}/review")
def review_draft(
    draft_id: str,
    command: ReviewCommand,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    repository = ContentRepository(session)
    row = repository.review_draft(draft_id, command)
    if command.action == "approve":
        DraftSourceSnapshotStore(settings, repository).delete_after_approval(draft_id)
    session.commit()
    return draft_to_dict(row, settings)


@router.post("/drafts/{draft_id}/rewrite")
async def rewrite_draft(
    draft_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    """用已保存的来源证据（README 快照 + 证据包）重写当前草稿正文。

    与聊天里的“重新生成”走同一条后台任务：不重新采集榜单、不新建草稿或生成记录，
    成功后覆盖为新的草稿版本；已有草稿的图片与审核记录保留。
    """
    repository = ContentRepository(session)
    draft = repository.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    if draft.status == ReviewStatus.PUBLISHED.value:
        raise HTTPException(status_code=409, detail="已发布的文案不能重写")
    origin_run = repository.find_generation_run_for_draft(draft_id)
    if origin_run is None:
        raise HTTPException(
            status_code=409,
            detail="当前草稿缺少可回溯的原生成记录，无法按已保存证据重写；请在生成记录中对该文案重新生成。",
        )
    assistant_message = repository.create_chat_message(
        origin_run.session_id,
        "assistant",
        f"已按你的要求用已保存的来源证据重写文案（当前版本 {draft.version}），将更新原草稿版本。",
    )
    repository.reopen_generation_run(origin_run, assistant_message.id, False, False)
    # 生成记录的状态要反映“正在重写”，而不是泛化的“正在原记录内重新生成”。
    origin_run.summary = "正在重写文案"
    repository.add_chat_agent_event(
        origin_run.id,
        "重写文案",
        f"使用已保存的 README 与证据包重写正文，目标草稿版本 {draft.version}；不重新采集、不新建草稿。",
        "running",
        metadata={"phase": "text", "state": "running", "draft_ids": [draft_id], "regeneration": True, "rewrite": True},
    )
    session.commit()
    try:
        job_id = await enqueue_draft_regeneration_job(
            settings, origin_run.id, origin_run.session_id, assistant_message.id, draft_id,
            origin_run.auto_review_requested, origin_run.auto_illustration_requested,
        )
        repository.add_chat_agent_event(
            origin_run.id, "正在重写文案", f"任务编号：{job_id}；将用已保存的 README 与证据包重写正文并覆盖为新版本。",
            metadata={"phase": "text", "state": "running", "job_id": job_id, "draft_ids": [draft_id], "regeneration": True},
        )
        session.commit()
        logger.info("draft_rewrite_enqueued draft_id=%s run_id=%s job_id=%s", draft_id, origin_run.id, job_id)
    except Exception as exc:
        logger.exception("draft_rewrite_enqueue_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        repository.update_chat_message(assistant_message.id, "重写任务未能入队，请稍后重试。")
        repository.finish_chat_agent_run(
            origin_run.id, assistant_message.id, ConversationRunStatus.FAILED, "重写任务入队失败", [], str(exc)
        )
        session.commit()
        raise HTTPException(status_code=503, detail="重写任务未能入队，请稍后重试") from exc
    return {"message": chat_message_to_dict(assistant_message), "execution": chat_agent_run_to_dict(origin_run, repository)}


@router.post("/drafts/{draft_id}/publication")
def record_manual_publication(
    draft_id: str,
    command: ManualPublicationCommand,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    """仅回填运营人员已完成的发布结果；不调用任何平台或账号。"""
    row = ContentRepository(session).record_manual_publication(draft_id, command)
    session.commit()
    return draft_to_dict(row, settings)


@router.get("/drafts/{draft_id}/illustrations")
def list_draft_illustrations(
    draft_id: str, settings: Annotated[Settings, Depends(get_settings)], session: SessionDependency,
) -> list[dict[str, Any]]:
    repository = ContentRepository(session)
    return [draft_illustration_to_dict(row, repository, settings) for row in repository.list_draft_illustrations(draft_id)]


@router.post("/drafts/{draft_id}/illustrations")
def attach_draft_illustration(
    draft_id: str, command: DraftIllustrationCreate,
    settings: Annotated[Settings, Depends(get_settings)], session: SessionDependency,
) -> dict[str, Any]:
    """将已有私有素材摆放到草稿指定段落，不上传公众号。"""
    repository = ContentRepository(session)
    row = repository.create_draft_illustration(
        draft_id, command.asset_id, command.purpose, command.placement_after_paragraph
    )
    session.commit()
    return draft_illustration_to_dict(row, repository, settings)


@router.patch("/drafts/{draft_id}/illustrations/{illustration_id}")
def move_draft_illustration(
    draft_id: str, illustration_id: str, command: DraftIllustrationCreate,
    settings: Annotated[Settings, Depends(get_settings)], session: SessionDependency,
) -> dict[str, Any]:
    repository = ContentRepository(session)
    # 前端一直会带上 purpose；此前只更新段位，导致“设为封面/改为正文”被静默忽略。
    row = repository.update_draft_illustration(
        draft_id, illustration_id, command.purpose, command.placement_after_paragraph
    )
    session.commit()
    return draft_illustration_to_dict(row, repository, settings)


@router.delete("/drafts/{draft_id}/illustrations/{illustration_id}")
def remove_draft_illustration(draft_id: str, illustration_id: str, session: SessionDependency) -> dict[str, str]:
    repository = ContentRepository(session)
    repository.delete_draft_illustration(draft_id, illustration_id)
    repository.invalidate_unfinished_wechat_publication_for_regeneration(
        draft_id,
        "当前文章配图已调整，原投递图片选择已失效；下次投递将只从当前保留图片重新确定。",
    )
    session.commit()
    return {"deleted_id": illustration_id}


@router.post("/drafts/{draft_id}/illustrations/generate")
async def generate_draft_illustration(
    draft_id: str, command: DraftIllustrationCreate,
    settings: Annotated[Settings, Depends(get_settings)], session: SessionDependency,
) -> dict[str, Any]:
    """显式请求生成草稿插图；不上传公众号也不发表。"""
    repository = ContentRepository(session)
    try:
        generated = await ImageGenerationTool(settings, repository).invoke(
            draft_id, command.purpose, command.placement_after_paragraph
        )
    except ImageGenerationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    row = next(item for item in repository.list_draft_illustrations(draft_id) if item.id == generated.illustration_id)
    return draft_illustration_to_dict(row, repository, settings)


@router.get("/drafts/{draft_id}/auto-reviews")
def list_auto_reviews(draft_id: str, session: SessionDependency) -> list[dict[str, Any]]:
    return [auto_review_to_dict(row) for row in ContentRepository(session).list_auto_review_runs(draft_id)]


@router.get("/drafts/{draft_id}/revisions")
def list_draft_revisions(draft_id: str, session: SessionDependency) -> list[dict[str, Any]]:
    return [draft_revision_to_dict(row) for row in ContentRepository(session).list_draft_revisions(draft_id)]


@router.post("/drafts/{draft_id}/auto-review")
async def run_auto_review(
    draft_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
    deliver: bool = Query(
        default=True,
        description="true=审核通过后按开关创建公众号草稿；false=仅审核与按意见改稿，不投递",
    ),
) -> dict[str, Any]:
    """创建可追溯审核任务并立即入队；通过不会自动发表。"""
    repository = ContentRepository(session)
    repository.expire_stale_auto_review_run(draft_id, settings.collection_job_timeout_seconds)
    session.commit()
    existing = repository.find_active_auto_review_run(draft_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="该文章已有自动审核任务正在处理中")
    run = repository.create_auto_review_run(draft_id, None, status="queued")
    session.commit()
    try:
        job_id = await enqueue_auto_review_job(settings, draft_id, run.id, deliver)
    except Exception as exc:
        repository.finish_auto_review_run(run.id, "failed", {}, {}, "自动审核任务入队失败，请稍后重试")
        session.commit()
        logger.exception("auto_review_enqueue_failed draft_id=%s review_id=%s error_type=%s", draft_id, run.id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="自动审核任务未能入队，请稍后重试") from exc
    logger.info("auto_review_queued draft_id=%s review_id=%s deliver=%s job_id=%s", draft_id, run.id, deliver, job_id)
    return {"review_id": run.id, "status": "queued", "draft_id": draft_id, "deliver": deliver}


def _ensure_wechat_image_attachment(row: AttachmentRow) -> None:
    suffix = Path(row.original_name).suffix.casefold()
    if suffix not in {".jpg", ".jpeg", ".png"} or row.content_type not in {
        "image/jpeg", "image/png"
    }:
        raise HTTPException(status_code=400, detail="公众号图片仅支持 JPG、JPEG、PNG 附件")


async def _read_wechat_image(settings: Settings, row: AttachmentRow) -> bytes:
    _ensure_wechat_image_attachment(row)
    try:
        return await run_in_threadpool(PrivateAttachmentStore(settings).read, row.object_key)
    except AttachmentError as exc:
        raise HTTPException(status_code=503, detail="图片附件暂时不可读取") from exc


def _ensure_wechat_publication_asset(row: PublicationAssetRow) -> None:
    suffix = Path(row.original_name).suffix.casefold()
    if suffix not in {".jpg", ".jpeg", ".png"} or row.content_type not in {
        "image/jpeg",
        "image/png",
    }:
        raise HTTPException(status_code=400, detail="发布图片仅支持 JPG、JPEG、PNG 文件")


async def _read_wechat_publication_asset(
    settings: Settings, row: PublicationAssetRow
) -> bytes:
    _ensure_wechat_publication_asset(row)
    try:
        return await run_in_threadpool(PrivateAttachmentStore(settings).read, row.object_key)
    except AttachmentError as exc:
        raise HTTPException(status_code=503, detail="发布图片暂时不可读取") from exc


@router.get("/wechat/image-attachments")
def list_wechat_image_attachments(
    session: SessionDependency,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    """发布页只读取图片元数据；文件仍留在私有 MinIO，未上传到公众号。"""
    repository = ContentRepository(session)
    return [attachment_to_dict(row, settings) for row in repository.list_image_attachments()]


@router.get("/wechat/assets")
def list_wechat_publication_assets(
    session: SessionDependency,
    settings: Annotated[Settings, Depends(get_settings)],
    draft_id: str = Query(min_length=1, max_length=36),
) -> list[dict[str, Any]]:
    """仅列出当前文章显式上传或已由 Agent 生成的私有素材。"""
    repository = ContentRepository(session)
    return [
        publication_asset_to_dict(row, settings)
        for row in repository.list_draft_publication_assets(draft_id)
    ]


@router.post("/wechat/assets")
async def upload_wechat_publication_asset(
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
    draft_id: Annotated[str, Form(min_length=1, max_length=36)],
    file: UploadFile = File(...),
) -> dict[str, Any]:
    repository = ContentRepository(session)
    repository.get_draft(draft_id)
    filename = file.filename or "publication-image.png"
    content_type = file.content_type or "application/octet-stream"
    content = await file.read(settings.attachment_max_bytes + 1)
    try:
        validate_image_attachment(filename, content, content_type, settings)
        object_key, content_hash = await run_in_threadpool(
            PrivateAttachmentStore(settings).upload, filename, content, content_type
        )
    except AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    asset = repository.create_publication_asset(
        original_name=filename,
        content_type=content_type,
        object_key=object_key,
        size_bytes=len(content),
        sha256=content_hash,
    )
    repository.bind_publication_asset(draft_id, asset.id)
    session.commit()
    logger.info("wechat_publication_asset_uploaded asset_id=%s draft_id=%s size_bytes=%s", asset.id, draft_id, len(content))
    return publication_asset_to_dict(asset, settings)


@router.delete("/wechat/assets/{asset_id}")
def delete_wechat_publication_asset(
    asset_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, str]:
    repository = ContentRepository(session)
    try:
        asset = repository.get_publication_asset(asset_id)
        repository.delete_publication_asset(asset_id)
        PrivateAttachmentStore(settings).delete(asset.object_key)
        session.commit()
    except PublicationAssetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AttachmentError as exc:
        session.rollback()
        raise HTTPException(status_code=503, detail="发布素材暂时无法删除") from exc
    except Exception:
        session.rollback()
        raise
    logger.info("wechat_publication_asset_deleted asset_id=%s", asset_id)
    return {"deleted_id": asset_id}


@router.get("/wechat/publications")
def list_wechat_publications(session: SessionDependency) -> list[dict[str, Any]]:
    repository = ContentRepository(session)
    return [
        wechat_publication_to_dict(row, repository)
        for row in repository.list_wechat_publications()
    ]


@router.get("/wechat/remote-drafts")
async def list_remote_wechat_drafts(
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
    offset: int = Query(default=0, ge=0),
    count: int = Query(default=20, ge=1, le=20),
) -> dict[str, Any]:
    """明确点击后才读取公众号草稿箱，不写入本地数据库。"""
    try:
        async with WechatOfficialAccountTool(settings) as client:
            total_count, items = await client.list_remote_drafts(offset=offset, count=count)
    except WechatOfficialAccountError as exc:
        logger.warning(
            "wechat_remote_drafts_list_failed category=%s provider_code=%s",
            exc.category,
            exc.provider_code,
        )
        repository = ContentRepository(session)
        repository.upsert_failure_notification(
            "wechat_remote_drafts", "remote-drafts", "wechat", "公众号草稿箱同步失败", str(exc), "publishing",
        )
        session.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"total_count": total_count, "items": [item.__dict__ for item in items]}


@router.get("/wechat/remote-publications")
async def list_remote_wechat_publications(
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
    offset: int = Query(default=0, ge=0),
    count: int = Query(default=20, ge=1, le=20),
) -> dict[str, Any]:
    """个人账号流程止于草稿箱；不查询未获授权的已发表内容接口。"""
    raise HTTPException(status_code=410, detail="当前账号流程止于草稿箱，且未授权查询已发表内容")


@router.post("/wechat/publications/{draft_id}/prepare")
async def prepare_wechat_publication(
    draft_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    """由 Agent 选择已生成图片并上传公众号素材；尚不创建图文草稿。"""
    repository = ContentRepository(session)
    try:
        job = await prepare_agent_selected_wechat_assets(settings, repository, draft_id)
    except (WechatOfficialAccountError, RuntimeError) as exc:
        logger.warning("wechat_agent_prepare_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        repository.upsert_failure_notification(
            "wechat_prepare", draft_id, "wechat", "公众号素材选择或上传失败", str(exc), "publishing",
        )
        session.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    session.commit()
    logger.info("wechat_agent_media_prepared draft_id=%s inline_image_count=%s", draft_id, len(job.inline_asset_ids_json or []))
    return wechat_publication_to_dict(job, repository)


@router.post("/wechat/publications/{job_id}/create-draft")
async def create_wechat_draft(
    job_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    """仅由发布页明确确认调用；将已审核草稿创建为微信公众号草稿箱记录。"""
    repository = ContentRepository(session)
    job = repository.get_wechat_publication(job_id)
    if job.wechat_draft_media_id:
        return wechat_publication_to_dict(job, repository)
    draft = repository.get_draft(job.draft_id)
    if not job.cover_media_id:
        raise HTTPException(status_code=409, detail="请先上传公众号封面图")
    try:
        async with WechatOfficialAccountTool(settings) as client:
            media_id = await client.create_draft(
                title=(draft.title_options_json or ["未命名草稿"])[0],
                digest=normalize_wechat_description(draft.summary_cn),
                content_html=render_wechat_html(draft.body, job.inline_image_urls_json or []),
                source_url=draft.source_url,
                cover_media_id=job.cover_media_id,
            )
    except WechatOfficialAccountError as exc:
        repository.mark_wechat_status(job_id, "draft_failed", error_message=str(exc))
        repository.upsert_failure_notification(
            "wechat_publication", job_id, "wechat", "公众号草稿创建失败", str(exc), "publishing",
        )
        session.commit()
        logger.warning("wechat_draft_create_failed job_id=%s", job_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    job = repository.mark_wechat_draft_created(job_id, media_id)
    session.commit()
    logger.info("wechat_draft_created job_id=%s", job_id)
    return wechat_publication_to_dict(job, repository)


@router.post("/wechat/publications/drafts/{draft_id}/retry")
async def retry_wechat_draft_delivery(
    draft_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    """重新投递已审核文章；复用正文与已有图片，绝不重新生成或重新审核。"""
    repository = ContentRepository(session)
    try:
        job = await retry_agent_selected_wechat_draft(settings, repository, draft_id)
    except (WechatOfficialAccountError, PublicationAssetSelectionError) as exc:
        logger.warning("wechat_draft_retry_request_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        repository.upsert_failure_notification(
            "wechat_retry", draft_id, "wechat", "公众号草稿重新投递失败", str(exc), "publishing",
        )
        session.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return wechat_publication_to_dict(job, repository)


@router.post("/wechat/publications/{job_id}/submit")
async def submit_wechat_publication(
    job_id: str,
) -> dict[str, Any]:
    """个人账号工作流止于草稿箱，禁止从本服务提交发表。"""
    raise HTTPException(status_code=410, detail="当前账号流程以公众号草稿箱为最终节点，不支持提交发表")


@router.post("/wechat/publications/{job_id}/refresh")
async def refresh_wechat_publication(
    job_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: SessionDependency,
) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="当前账号流程以公众号草稿箱为最终节点，不查询发布状态")
