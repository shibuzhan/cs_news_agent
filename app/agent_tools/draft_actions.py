"""草稿动作 Tool：审核、重写、审核决定、配图都由会话 Agent 调用，而不是前端直连专用接口。

设计边界：
- 只允许操作**本会话的当前文章**，或本会话生成过的草稿（用 `draft_id` 显式指定并校验归属）；
- 长任务只负责入队（ARQ），不在工具里同步等待模型；
- 对外副作用（创建公众号草稿）不在这里，留在受控投递接口与开关之后。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from app.config import Settings, get_settings
from app.domain.models import ConversationIntent, ReviewCommand, ReviewStatus
from app.jobs import (
    enqueue_auto_review_job,
    enqueue_draft_regeneration_job,
    enqueue_image_generation_job,
)
from app.storage.database import SessionLocal
from app.services.async_bridge import run_coroutine_sync
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.draft_actions")

EDITABLE_STATUSES = {ReviewStatus.PENDING_REVIEW.value, ReviewStatus.NEEDS_REVISION.value}
REVIEWABLE_STATUSES = EDITABLE_STATUSES | {ReviewStatus.READY_TO_PUBLISH.value}
MAX_PLACEMENT = 20


def _draft_belongs_to_session(repository: ContentRepository, session_id: str, draft_id: str) -> bool:
    """草稿归属校验：只能操作本会话生成过的草稿。"""
    run = repository.find_generation_run_for_draft(draft_id)
    return bool(run is not None and run.session_id == session_id)


def _resolve_draft(
    repository: ContentRepository, session_id: str, draft_id: str = "", *, allowed: set[str] | None = None
) -> tuple[Any | None, str]:
    """解析目标草稿：显式 id 需校验归属；未给 id 时用本会话当前文章。"""
    if draft_id:
        try:
            draft = repository.get_draft(draft_id)
        except Exception:
            return None, "找不到这篇草稿。"
        if not _draft_belongs_to_session(repository, session_id, draft_id):
            return None, "该草稿不属于本会话，不能在这里操作。"
    else:
        memory = repository.get_chat_session_memory(session_id)
        if not memory.active_draft_id:
            return None, "本会话还没有当前文章：请先明确指定要操作的草稿。"
        draft = repository.get_draft(memory.active_draft_id)
    if allowed is not None and draft.status not in allowed:
        return None, f"当前状态（{draft.status}）不支持这个操作。"
    return draft, ""


def _draft_label(draft: Any) -> str:
    return (draft.title_options_json or ["当前草稿"])[0]


def _announce(repository: ContentRepository, session_id: str, run: Any, pending: str) -> str:
    """给工具发起的运行挂一条助手消息。

    后台任务结束时 `_report_*` 会更新这条消息，用户才能在**对话里**看到结果；
    否则结果只留在运行摘要里，看起来像“没有回复”（真实反馈：报告不在聊天框里）。
    """
    message = repository.create_chat_message(session_id, "assistant", pending)
    run.response_message_id = message.id
    repository.session.flush()
    return message.id


async def _start_review(
    settings: Settings, repository: ContentRepository, session_id: str, draft: Any, *, deliver: bool
) -> dict[str, Any]:
    from app.services.pending_reviews import ACTIVE_IMAGE_STATUSES, mark_pending_review

    repository.expire_stale_auto_review_run(draft.id, settings.collection_job_timeout_seconds)
    existing = repository.find_active_auto_review_run(draft.id)
    if existing is not None:
        return {"status": "already_running", "message": "这篇的自动审核已经在处理中。", "draft_id": draft.id}
    # 配图还在生成时不立刻审核：否则审核会看到“还没有正文插图”的草稿，
    # 汇报也会与实际时序矛盾（真实反馈：“感觉这样不是很合理”）。
    active_images = repository.count_active_image_jobs(draft.id, ACTIVE_IMAGE_STATUSES)
    chat_run = repository.create_chat_agent_run(session_id, None, ConversationIntent.RUN_AUTO_REVIEW, False, False)
    if active_images:
        mark_pending_review(repository, draft.id, deliver=deliver, chat_run_id=chat_run.id)
        _announce(
            repository, session_id, chat_run,
            f"《{_draft_label(draft)}》还有 {active_images} 个配图任务在生成，配图完成后我会自动开始审核。",
        )
        repository.add_chat_agent_event(
            chat_run.id, "审核已排队（等待配图）",
            f"当前有 {active_images} 个配图任务未完成；它们结束后自动开始审核，无需再提醒我。",
            "running",
            metadata={"phase": "review", "state": "queued", "draft_ids": [draft.id], "pending_images": active_images},
        )
        return {
            "status": "queued_after_images",
            "draft_id": draft.id,
            "draft_title": _draft_label(draft),
            "pending_images": active_images,
            "chat_run_id": chat_run.id,
            "message": f"配图完成后会自动审核《{_draft_label(draft)}》（当前还有 {active_images} 个配图任务）。",
        }
    _announce(repository, session_id, chat_run, f"好，正在审核《{_draft_label(draft)}》；通过后不会自动发表。")
    review_run = repository.create_auto_review_run(draft.id, None, status="queued")
    repository.add_chat_agent_event(
        chat_run.id, "自动审核中",
        "正在按规则与模型审核当前文案与配图，并按意见改稿一轮。",
        "running",
        metadata={"phase": "review", "state": "running", "draft_ids": [draft.id], "review_id": review_run.id},
    )
    job_id = await enqueue_auto_review_job(settings, draft.id, review_run.id, deliver, chat_run.id)
    repository.add_chat_agent_event(
        chat_run.id, "审核任务已创建", f"任务编号：{job_id}",
        metadata={"phase": "review", "state": "running", "job_id": job_id, "draft_ids": [draft.id]},
    )
    return {
        "status": "started",
        "draft_id": draft.id,
        "draft_title": _draft_label(draft),
        "deliver": deliver,
        "review_id": review_run.id,
        "chat_run_id": chat_run.id,
        "message": f"已开始审核《{_draft_label(draft)}》"
        + ("（通过后会创建公众号草稿，不会发表）" if deliver else "（仅审核，不投递）"),
    }


async def _start_rewrite(
    settings: Settings, repository: ContentRepository, session_id: str, draft: Any
) -> dict[str, Any]:
    origin_run = repository.find_generation_run_for_draft(draft.id)
    if origin_run is None:
        return {"status": "rejected", "message": "这篇缺少可回溯的原生成记录，无法按已保存证据重写。"}
    assistant_message = repository.create_chat_message(
        session_id, "assistant", f"正在用已保存的 README 与证据包重写《{_draft_label(draft)}》…"
    )
    repository.reopen_generation_run(origin_run, assistant_message.id, False, False)
    origin_run.summary = "正在重写文案"
    repository.add_chat_agent_event(
        origin_run.id, "重写文案",
        f"使用已保存的 README 与证据包重写正文，目标草稿版本 {draft.version}；不重新采集、不新建草稿。",
        "running",
        metadata={"phase": "text", "state": "running", "draft_ids": [draft.id], "regeneration": True, "rewrite": True},
    )
    job_id = await enqueue_draft_regeneration_job(
        settings, origin_run.id, session_id, assistant_message.id, draft.id,
        origin_run.auto_review_requested, origin_run.auto_illustration_requested,
    )
    repository.add_chat_agent_event(
        origin_run.id, "正在重写文案", f"任务编号：{job_id}；完成后会覆盖为新的草稿版本。",
        metadata={"phase": "text", "state": "running", "job_id": job_id, "draft_ids": [draft.id], "rewrite": True},
    )
    return {
        "status": "started",
        "draft_id": draft.id,
        "draft_title": _draft_label(draft),
        "run_id": origin_run.id,
        "message": f"已开始用已保存的来源证据重写《{_draft_label(draft)}》，完成后覆盖为新版本。",
    }


def build_draft_action_tools(session_id: str):
    """构造只作用于本会话草稿的动作 Tool。"""

    async def _impl_run_auto_review(draft_id: str = "", deliver: bool = False) -> dict[str, Any]:
        """对当前文章发起自动审核（规则 + 模型审核，并按意见改稿一轮）。

        deliver=true 表示审核通过后创建公众号草稿箱记录（不会发表）；默认 false 仅审核。
        """
        settings = get_settings()
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=REVIEWABLE_STATUSES)
            if draft is None:
                return {"status": "rejected", "message": reason}
            result = await _start_review(settings, repository, session_id, draft, deliver=deliver)
            session.commit()
        logger.info("agent_tool_run_auto_review session_id=%s draft_id=%s status=%s", session_id, draft_id or "-", result.get("status"))
        return result

    async def _impl_rewrite_draft(draft_id: str = "") -> dict[str, Any]:
        """用已保存的来源证据重写当前文案正文（不重新采集、不新建草稿），覆盖为新版本。"""
        settings = get_settings()
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=EDITABLE_STATUSES)
            if draft is None:
                return {"status": "rejected", "message": reason}
            result = await _start_rewrite(settings, repository, session_id, draft)
            session.commit()
        logger.info("agent_tool_rewrite_draft session_id=%s draft_id=%s status=%s", session_id, draft_id or "-", result.get("status"))
        return result

    def _review_decision(draft_id: str, action: str, note: str, allowed: set[str]) -> dict[str, Any]:
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=allowed)
            if draft is None:
                return {"status": "rejected", "message": reason}
            row = repository.review_draft(
                draft.id,
                ReviewCommand(
                    reviewer="会话 Agent",
                    action=action,
                    note=note or f"由会话 Agent 执行 {action}",
                    idempotency_key=f"agent-{draft.id}-{action}",
                ),
            )
            session.commit()
            label = {"approve": "审核通过", "discard": "废弃文案", "revoke": "撤销审核"}.get(action, action)
            return {"status": "done", "draft_id": row.id, "draft_status": row.status, "draft_title": _draft_label(row),
                    "message": f"已对《{_draft_label(row)}》执行：{label}。"}

    @tool("approve_draft")
    def approve_draft(draft_id: str = "", note: str = "") -> dict[str, Any]:
        """把当前文章标记为审核通过（不会自动发表，也不会创建公众号草稿）。"""
        return _review_decision(draft_id, "approve", note, EDITABLE_STATUSES)

    @tool("discard_draft")
    def discard_draft(draft_id: str = "", note: str = "") -> dict[str, Any]:
        """废弃当前文章；来源与审核记录仍可追溯。"""
        return _review_decision(draft_id, "discard", note, REVIEWABLE_STATUSES)

    @tool("revoke_approval")
    def revoke_approval(draft_id: str = "", note: str = "") -> dict[str, Any]:
        """撤销已通过的审核，使文案回到可编辑状态。"""
        return _review_decision(draft_id, "revoke", note, {ReviewStatus.READY_TO_PUBLISH.value})

    async def _impl_generate_draft_illustration(
        purpose: str = "cover", placement_after_paragraph: int = 0, draft_id: str = ""
    ) -> dict[str, Any]:
        """为当前文章生成一张配图：purpose 为 cover 或 inline，inline 需给段位。

        只生成并私有保存，不上传公众号、不发表。
        """
        settings = get_settings()
        resolved = purpose if purpose in {"cover", "inline"} else "cover"
        placement = 0 if resolved == "cover" else max(0, min(int(placement_after_paragraph), MAX_PLACEMENT))
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=REVIEWABLE_STATUSES)
            if draft is None:
                return {"status": "rejected", "message": reason}
            chat_run = repository.create_chat_agent_run(session_id, None, ConversationIntent.GENERATE_DRAFT_IMAGE, False, True)
            _announce(
                repository, session_id, chat_run,
                f"好，正在为《{_draft_label(draft)}》生成{'封面图' if resolved == 'cover' else '正文插图'}。",
            )
            from app.tools.illustration_planner import style_for, subject_for

            task = repository.create_image_generation_job(
                chat_run.id, draft.id, resolved, placement,
                subject_for(draft, resolved), style_for(draft, resolved),
            )
            repository.add_chat_agent_event(
                chat_run.id, "图片任务已入队",
                f"正在生成{'封面图' if resolved == 'cover' else f'第 {placement} 段后的正文插图'}。",
                "running",
                metadata={"phase": "image", "state": "queued", "draft_ids": [draft.id], "task_id": task.id},
            )
            session.commit()
        arq_job_id = await enqueue_image_generation_job(settings, task.id)
        with SessionLocal() as session:
            ContentRepository(session).update_image_generation_job(task.id, "queued", arq_job_id=arq_job_id)
            session.commit()
        logger.info("agent_tool_generate_illustration session_id=%s draft_id=%s purpose=%s", session_id, draft.id, resolved)
        return {
            "status": "started",
            "draft_id": draft.id,
            "draft_title": _draft_label(draft),
            "purpose": resolved,
            "placement_after_paragraph": placement,
            "task_id": task.id,
            "chat_run_id": chat_run.id,
            "message": f"已开始为《{_draft_label(draft)}》生成{'封面图' if resolved == 'cover' else '正文插图'}。",
        }

    async def _impl_publish_to_wechat_draft(draft_id: str = "") -> dict[str, Any]:
        """把当前已审核通过的文章投递到微信公众号草稿箱（绝不发表）。

        远端草稿已存在时会**原地覆盖**内容与配图（`draft/update`），不会留下重复草稿；
        文案或配图在投递后被修改过时，这一步会把远端草稿刷新到最新内容。
        """
        settings = get_settings()
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(
                repository, session_id, draft_id,
                allowed={ReviewStatus.READY_TO_PUBLISH.value, ReviewStatus.DRAFTBOX_CREATED.value},
            )
            if draft is None:
                return {"status": "rejected", "message": reason}
            if not settings.auto_wechat_draft_enabled:
                return {"status": "rejected", "message": "公众号草稿投递开关未启用（AUTO_WECHAT_DRAFT_ENABLED=false）。"}
            existing = repository.get_wechat_publication_for_draft(draft.id)
            updating = bool(existing is not None and existing.wechat_draft_media_id)
            # 投递要上传图片并调用微信写接口，耗时远超对话请求时限：只入队，由后台完成并汇报。
            chat_run = repository.create_chat_agent_run(session_id, None, ConversationIntent.PUBLISH_TO_WECHAT_DRAFT, False, False)
            _announce(
                repository, session_id, chat_run,
                f"好，正在{'更新' if updating else '创建'}公众号草稿（{_draft_label(draft)}）；不会发表，完成后我会汇报。",
            )
            repository.add_chat_agent_event(
                chat_run.id, "投递任务已入队",
                f"将{'原地覆盖' if updating else '创建'}公众号草稿（{_draft_label(draft)}），不会发表。",
                "running",
                metadata={"phase": "text", "state": "queued", "draft_ids": [draft.id]},
            )
            session.commit()
        try:
            from app.jobs import enqueue_wechat_delivery_job

            job_id = await enqueue_wechat_delivery_job(settings, draft.id, chat_run.id)
        except Exception as exc:
            logger.warning("agent_tool_publish_enqueue_failed draft_id=%s error_type=%s", draft.id, type(exc).__name__)
            return {"status": "failed", "draft_id": draft.id, "message": f"投递任务未能入队：{exc}"}
        with SessionLocal() as session:
            repository = ContentRepository(session)
            repository.add_chat_agent_event(
                chat_run.id, "投递任务已创建", f"任务编号：{job_id}",
                metadata={"phase": "text", "state": "running", "job_id": job_id, "draft_ids": [draft.id]},
            )
            session.commit()
        logger.info("agent_tool_publish_enqueued draft_id=%s updating=%s job_id=%s", draft.id, updating, job_id)
        return {
            "status": "started",
            "draft_id": draft.id,
            "draft_title": _draft_label(draft),
            "updated_remote": updating,
            "chat_run_id": chat_run.id,
            "message": f"已开始{'更新' if updating else '创建'}公众号草稿（{_draft_label(draft)}），完成后我会汇报；不会发表。",
        }

    async def _impl_reselect_publication_assets(draft_id: str = "") -> dict[str, Any]:
        """作废当前投递素材选择并按最新口径重新选择（真实截图优先、AI 配图补足）。

        已投递的草稿会在重选后**原地覆盖**远端公众号草稿；未投递时只固化新的选择。
        """
        settings = get_settings()
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(
                repository, session_id, draft_id,
                allowed={ReviewStatus.READY_TO_PUBLISH.value, ReviewStatus.DRAFTBOX_CREATED.value},
            )
            if draft is None:
                return {"status": "rejected", "message": reason}
            existing = repository.get_wechat_publication_for_draft(draft.id)
            updating = bool(existing is not None and existing.wechat_draft_media_id)
            # 作废旧选择：这样后台投递一定会重新跑素材选择（含视觉识别官方图）。
            invalidated = repository.invalidate_unfinished_wechat_publication_for_regeneration(
                draft.id, "用户要求按最新口径重新选择投递配图。"
            )
            chat_run = repository.create_chat_agent_run(
                session_id, None, ConversationIntent.RESELECT_PUBLICATION_ASSETS, False, False
            )
            _announce(
                repository, session_id, chat_run,
                f"好，正在重新为《{_draft_label(draft)}》选择投递配图（真实截图优先，AI 配图补足）"
                + ("，完成后会覆盖公众号草稿内容。" if updating else "。"),
            )
            repository.add_chat_agent_event(
                chat_run.id, "重新选择配图已入队",
                "已作废旧的投递素材选择，将按当前草稿里的全部图片重新选择。",
                "running",
                metadata={"phase": "image", "state": "queued", "draft_ids": [draft.id], "reselection": True},
            )
            session.commit()
        try:
            from app.jobs import enqueue_wechat_delivery_job

            job_id = await enqueue_wechat_delivery_job(settings, draft.id, chat_run.id)
        except Exception as exc:
            logger.warning("agent_tool_reselect_enqueue_failed draft_id=%s error_type=%s", draft.id, type(exc).__name__)
            return {"status": "failed", "draft_id": draft.id, "message": f"重新选择配图任务未能入队：{exc}"}
        with SessionLocal() as session:
            repository = ContentRepository(session)
            repository.add_chat_agent_event(
                chat_run.id, "任务已创建", f"任务编号：{job_id}",
                metadata={"phase": "image", "state": "running", "job_id": job_id, "draft_ids": [draft.id]},
            )
            session.commit()
        logger.info(
            "agent_tool_reselect_publication_assets draft_id=%s invalidated=%s updating=%s job_id=%s",
            draft.id, invalidated, updating, job_id,
        )
        return {
            "status": "started",
            "draft_id": draft.id,
            "draft_title": _draft_label(draft),
            "previous_selection_invalidated": invalidated,
            "updated_remote": updating,
            "chat_run_id": chat_run.id,
            "message": f"已开始重新选择《{_draft_label(draft)}》的投递配图，完成后我会汇报。",
        }

    # 会话 Agent 以同步方式执行工具：异步实现必须配同步外壳，否则 LangChain 抛
    # NotImplementedError（真实故障：对话模型报“暂时不可用”，failure_stage=agent_invoke）。
    @tool("run_auto_review")
    def run_auto_review(draft_id: str = "", deliver: bool = False) -> dict[str, Any]:
        """对当前文章发起自动审核（规则 + 模型审核，并按意见改稿一轮）。

        deliver=true 表示审核通过后创建公众号草稿箱记录（不会发表）；默认 false 仅审核。
        """
        return run_coroutine_sync(_impl_run_auto_review(draft_id=draft_id, deliver=deliver))

    @tool("rewrite_draft")
    def rewrite_draft(draft_id: str = "") -> dict[str, Any]:
        """用已保存的来源证据重写当前文案正文（不重新采集、不新建草稿），覆盖为新版本。"""
        return run_coroutine_sync(_impl_rewrite_draft(draft_id=draft_id))

    @tool("generate_draft_illustration")
    def generate_draft_illustration(
        purpose: str = "cover", placement_after_paragraph: int = 0, draft_id: str = ""
    ) -> dict[str, Any]:
        """为当前文章生成一张配图：purpose 为 cover 或 inline，inline 需给段位。

        只生成并私有保存，不上传公众号、不发表。
        """
        return run_coroutine_sync(
            _impl_generate_draft_illustration(
                purpose=purpose, placement_after_paragraph=placement_after_paragraph, draft_id=draft_id
            )
        )

    @tool("publish_to_wechat_draft")
    def publish_to_wechat_draft(draft_id: str = "") -> dict[str, Any]:
        """把当前已审核通过的文章投递到微信公众号草稿箱（绝不发表）。

        远端草稿已存在时会**原地覆盖**内容与配图（`draft/update`），不会留下重复草稿；
        文案或配图在投递后被修改过时，这一步会把远端草稿刷新到最新内容。
        """
        return run_coroutine_sync(_impl_publish_to_wechat_draft(draft_id=draft_id))

    @tool("reselect_publication_assets")
    def reselect_publication_assets(draft_id: str = "") -> dict[str, Any]:
        """重新选择投递配图：作废旧选择并按“真实截图优先、AI 配图补足”重选，必要时覆盖远端草稿。"""
        return run_coroutine_sync(_impl_reselect_publication_assets(draft_id=draft_id))

    return [
        run_auto_review,
        rewrite_draft,
        approve_draft,
        discard_draft,
        revoke_approval,
        generate_draft_illustration,
        publish_to_wechat_draft,
        reselect_publication_assets,
    ]
