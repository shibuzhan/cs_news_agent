"""草稿动作 Tool：审核、重写、审核决定、配图都由会话 Agent 调用，而不是前端直连专用接口。

设计边界：
- 只允许操作**本会话的当前文章**，或本会话生成过的草稿（用 `draft_id` 显式指定并校验归属）；
- 长任务只负责入队（ARQ），不在工具里同步等待模型；
- 对外副作用（创建公众号草稿）不在这里，留在受控投递接口与开关之后。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import tool

from app.config import Settings, get_settings
from app.domain.models import (
    ConversationIntent,
    ConversationRunStatus,
    RawSourceItem,
    ReviewCommand,
    ReviewStatus,
    SourceKind,
)
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
    """草稿归属校验：只能操作本会话正在使用的草稿。

    判定口径见 `ContentRepository.session_references_draft`：当前文章、生成记录，
    或本会话任意运行事件里出现过的草稿都算；本会话从未碰过的草稿仍然拒绝。
    """
    return repository.session_references_draft(session_id, draft_id)


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
            return None, (
                f"《{_draft_label(draft)}》不属于本会话，不能在这里操作。"
                "如果确实要改这一篇，先让我把它设为本会话当前文章，再重试。"
            )
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

    受理运行（HTTP 入队时已经写过确定性回执）不再追加“好，正在…”：回执已经说明了这一步，
    重复气泡只会让对话变长（真实反馈：任务详情太啰嗦）。此时把这句话降级成运行事件。
    """
    if run.response_message_id:
        repository.add_chat_agent_event(
            run.id, "已开始处理", pending, "running",
            metadata={"phase": "intake", "state": "running"},
        )
        return run.response_message_id
    message = repository.create_chat_message(session_id, "assistant", pending)
    run.response_message_id = message.id
    repository.session.flush()
    return message.id


def _task_run(
    repository: ContentRepository,
    session_id: str,
    chat_run_id: str | None,
    intent: ConversationIntent,
    *,
    auto_illustration: bool = False,
) -> Any:
    """复用用户消息的受理运行（有则改意图），否则新建一条。

    同一条用户消息只应该有一条运行：否则“生成记录”里会出现两条，对话里也会出现两张卡片。
    """
    if chat_run_id:
        run = repository.get_chat_agent_run(chat_run_id)
        if run.status == ConversationRunStatus.RUNNING.value:
            run.intent = intent.value if isinstance(intent, ConversationIntent) else str(intent)
            return run
    return repository.create_chat_agent_run(session_id, None, intent, False, auto_illustration)


async def start_auto_review(
    settings: Settings, repository: ContentRepository, session_id: str, draft: Any, *,
    deliver: bool, chat_run_id: str | None = None,
) -> dict[str, Any]:
    """自动审核的公开入口：对话派发与 Agent 工具共用同一实现。"""
    return await _start_review(
        settings, repository, session_id, draft, deliver=deliver, chat_run_id=chat_run_id
    )


async def start_draft_rewrite(
    settings: Settings, repository: ContentRepository, session_id: str, draft: Any, *,
    origin_run: Any | None = None, chat_run_id: str | None = None, source_mode: str = "auto",
) -> dict[str, Any]:
    """按已保存来源证据重写的公开入口（`source_mode='snapshot'` 表示不联网重抓）。"""
    return await _start_rewrite(
        settings, repository, session_id, draft,
        origin_run=origin_run, chat_run_id=chat_run_id, source_mode=source_mode,
    )


async def _start_review(
    settings: Settings, repository: ContentRepository, session_id: str, draft: Any, *,
    deliver: bool, chat_run_id: str | None = None, revise: bool = True,
) -> dict[str, Any]:
    from app.services.pending_reviews import ACTIVE_IMAGE_STATUSES, mark_pending_review

    repository.expire_stale_auto_review_run(draft.id, settings.collection_job_timeout_seconds)
    existing = repository.find_active_auto_review_run(draft.id)
    if existing is not None:
        return {"status": "already_running", "message": "这篇的自动审核已经在处理中。", "draft_id": draft.id}
    # 正文正在被重写时也不立刻审核：否则审核的是**马上要被替换掉的那一版**，
    # 真实后果（2026-09-14 09:53）：审核 v1 → 改稿写 v2 → 重写写 v3 → 两个写者抢版本号，
    # 最后一条以唯一冲突失败，用户看到“生成失败”而正文其实已经改了。
    active_rewrite = repository.find_active_regeneration_run(
        draft.id,
        within_seconds=getattr(settings, "collection_job_timeout_seconds", 900),
        exclude_run_id=chat_run_id or "",
    )
    chat_run = _task_run(repository, session_id, chat_run_id, ConversationIntent.RUN_AUTO_REVIEW)
    if active_rewrite is not None:
        mark_pending_review(repository, draft.id, deliver=deliver, chat_run_id=chat_run.id, revise=revise)
        _announce(
            repository, session_id, chat_run,
            f"《{_draft_label(draft)}》正在重写正文，我等新版本生成后自动开始审核，不用再提醒我。",
        )
        repository.add_chat_agent_event(
            chat_run.id, "审核已排队（等待重写）",
            "当前正文正在被重写；重写完成后自动按**新版本**开始审核，避免审到即将被替换的旧版本。",
            "running",
            metadata={
                "phase": "review", "state": "queued", "draft_ids": [draft.id],
                "pending_rewrite_run_id": active_rewrite.id, "revise": revise,
            },
        )
        return {
            "status": "queued_after_rewrite",
            "status_text": "审核已排队（等待重写）",
            "draft_id": draft.id,
            "draft_title": _draft_label(draft),
            "pending_rewrite_run_id": active_rewrite.id,
            "chat_run_id": chat_run.id,
            "message": (
                f"正文正在重写；新版本生成后会自动审核《{_draft_label(draft)}》，"
                "这样审的就是新版本而不是即将被替换的旧版本。"
            ),
        }
    active_images = repository.count_active_image_jobs(draft.id, ACTIVE_IMAGE_STATUSES)
    if active_images:
        mark_pending_review(repository, draft.id, deliver=deliver, chat_run_id=chat_run.id, revise=revise)
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
            "status_text": "审核已排队（等待配图）",
            "draft_id": draft.id,
            "draft_title": _draft_label(draft),
            "pending_images": active_images,
            "chat_run_id": chat_run.id,
            "message": f"配图完成后会自动审核《{_draft_label(draft)}》（当前还有 {active_images} 个配图任务）。",
        }
    _announce(
        repository, session_id, chat_run,
        f"好，正在审核《{_draft_label(draft)}》；通过后不会自动发表。"
        if revise
        else f"好，正在审核《{_draft_label(draft)}》，只出意见、不改稿。",
    )
    review_run = repository.create_auto_review_run(draft.id, None, status="queued")
    repository.add_chat_agent_event(
        chat_run.id, "自动审核中",
        "正在按规则与模型审核当前文案与配图，并按意见改稿一轮。"
        if revise
        else "正在按规则与模型审核当前文案与配图；只出意见，不改稿。",
        "running",
        metadata={"phase": "review", "state": "running", "draft_ids": [draft.id], "review_id": review_run.id, "revise": revise},
    )
    job_id = await enqueue_auto_review_job(
        settings, draft.id, review_run.id, deliver, chat_run.id, revise
    )
    repository.add_chat_agent_event(
        chat_run.id, "审核任务已创建", f"任务编号：{job_id}",
        metadata={"phase": "review", "state": "running", "job_id": job_id, "draft_ids": [draft.id]},
    )
    return {
        "status": "started",
        "status_text": "审核中（不改稿）" if not revise else "审核中",
        "draft_id": draft.id,
        "draft_title": _draft_label(draft),
        "deliver": deliver,
        "revise": revise,
        "review_id": review_run.id,
        "chat_run_id": chat_run.id,
        "message": f"已开始审核《{_draft_label(draft)}》"
        + (
            "（只出意见、不改稿）"
            if not revise
            else "（通过后会创建公众号草稿，不会发表）" if deliver else "（仅审核，不投递）"
        ),
    }


def _already_rewriting(
    draft: Any, label: str, run_id: str, *, requested_by_another: bool,
) -> dict[str, Any]:
    """去重命中时的统一返回：不再入队、不再记事件，让用户看到一句准确的状态。"""
    reason = "另一个请求" if requested_by_another else "同一条请求的另一条执行路径"
    return {
        "status": "already_running",
        "status_text": "重写已在进行中",
        "draft_id": draft.id,
        "draft_title": label,
        "run_id": run_id,
        "message": (
            f"《{label}》已经在重写中（{reason}已经启动过这个任务，同一次改写只跑一个任务），"
            "完成后我会在同一张卡片上汇报。"
        ),
    }


async def _start_rewrite(
    settings: Settings, repository: ContentRepository, session_id: str, draft: Any, *,
    origin_run: Any | None = None, chat_run_id: str | None = None, source_mode: str = "auto",
) -> dict[str, Any]:
    """按已保存的来源证据重写正文。

    只要求“有来源快照/证据”，**不再要求有原生成记录**：更早生成的草稿没有 collect_news 运行审计
    （`draft_ids` 是后加的字段），旧逻辑会因此拒绝（用户实测：点“重写文案”报“这篇缺少可回溯的
    原生成记录”）。没有原记录时，报告落在当前对话的受理运行上，用户看到的仍是同一张卡片。

    `source_mode` 透传给后台任务：`snapshot` 只用已保存证据（用户先刷新过来源时用，避免重写时
    又抓到不同版本的 README），`auto` 才在缺快照时联网重抓。

    **同一篇草稿同时只允许一个重写任务**：同一条用户消息可能两条路径各入队一次（模型直接调
    `rewrite_draft` 工具 + 意图兜底再入队），两个任务并发写同一草稿会撞上草稿版本唯一约束，
    后完成的那次以 IntegrityError 失败——内容已经写进去了，用户却收到“生成失败”
    （真实故障：用户报“聊天框异常”，日志里 `uq_draft_revision_version` 冲突）。
    已在跑时复用同一个任务，不重复入队、不重复记事件。
    """
    mode = source_mode if source_mode in {"auto", "snapshot"} else "auto"
    source_note = "已保存的来源正文" if mode == "snapshot" else "已保存的 README 与证据包"
    label = _draft_label(draft)
    if origin_run is None:
        origin_run = repository.find_generation_run_for_draft(draft.id)
    chat_run = (
        _task_run(repository, session_id, chat_run_id, ConversationIntent.REGENERATE_DRAFT)
        if chat_run_id
        else None
    )
    if mode != "snapshot":
        planned_job_id = ""
        if chat_run is not None:
            # 同一条用户消息会两条路径各调一次（模型工具 + 意图兜底）：第一次已经入队过，
            # 第二次必须直接复用，否则两个任务并发写同一草稿（真实故障：聊天框里
            # 两个“正在处理中”，后完成的任务以草稿版本唯一冲突失败）。
            if getattr(chat_run, "rewrite_job_id", None):
                return _already_rewriting(draft, label, chat_run.id, requested_by_another=False)
            repository.claim_run_target_draft(chat_run.id, draft.id)
            planned_job_id = chat_run.id
        # 其它运行正在写同一篇：同一次点击只应该有一个任务（按草稿判断，不按会话）。
        active = repository.find_active_regeneration_run(
            draft.id,
            within_seconds=getattr(settings, "collection_job_timeout_seconds", 900),
            exclude_run_id=planned_job_id,
        )
        if active is not None:
            logger.info(
                "rewrite_dedup_skipped session_id=%s draft_id=%s active_run_id=%s",
                session_id, draft.id, active.id,
            )
            return _already_rewriting(draft, label, active.id, requested_by_another=True)
    if chat_run is not None:
        anchor_message_id = _announce(
            repository, session_id, chat_run, f"正在用{source_note}重写《{label}》…"
        )
    else:
        anchor_message_id = repository.create_chat_message(
            session_id, "assistant", f"正在用{source_note}重写《{label}》…"
        ).id

    if origin_run is not None:
        # 报告写回原生成记录：受理运行只作为对话锚点，随即结束，避免挂着一张“处理中”的卡片。
        repository.reopen_generation_run(origin_run, anchor_message_id, False, False)
        origin_run.summary = "正在重写文案"
        job_run_id = origin_run.id
        auto_review_requested = bool(origin_run.auto_review_requested)
        auto_illustration_requested = bool(origin_run.auto_illustration_requested)
        if chat_run is not None:
            repository.finish_chat_agent_run(
                chat_run.id, anchor_message_id, ConversationRunStatus.COMPLETED,
                f"已转交重写任务：{label}", [{"tool": "rewrite_draft", "draft_id": draft.id}],
            )
    else:
        # 没有原记录：受理运行保持“处理中”，由重写任务在同一张卡片上汇报并收尾。
        if chat_run is not None:
            job_run_id = chat_run.id
        else:
            fallback_run = repository.create_chat_agent_run(
                session_id, None, ConversationIntent.REGENERATE_DRAFT, False, False
            )
            fallback_run.response_message_id = anchor_message_id
            repository.session.flush()
            job_run_id = fallback_run.id
        job_run = repository.get_chat_agent_run(job_run_id)
        auto_review_requested = bool(getattr(job_run, "auto_review_requested", False))
        auto_illustration_requested = bool(getattr(job_run, "auto_illustration_requested", False))

    repository.add_chat_agent_event(
        job_run_id, "重写文案",
        f"使用{source_note}重写《{label}》正文，目标草稿版本 {draft.version}；不重新采集、不新建草稿。",
        "running",
        metadata={
            "phase": "text", "state": "running", "draft_ids": [draft.id],
            "regeneration": True, "rewrite": True, "source_mode": mode,
        },
    )
    job_id = await enqueue_draft_regeneration_job(
        settings, job_run_id, session_id, anchor_message_id, draft.id,
        auto_review_requested, auto_illustration_requested, mode,
    )
    # 入队成功立刻登记（调用方随后会 commit）：同一条消息的第二次请求据此判断“已经入队过”。
    if chat_run is not None:
        repository.mark_rewrite_job_enqueued(chat_run.id, draft.id, job_id)
    repository.add_chat_agent_event(
        job_run_id, "正在重写文案", f"任务编号：{job_id}；完成后会覆盖为新的草稿版本。",
        metadata={"phase": "text", "state": "running", "job_id": job_id, "draft_ids": [draft.id], "rewrite": True},
    )
    return {
        "status": "started",
        "status_text": "重写已开始",
        "draft_id": draft.id,
        "draft_title": label,
        "run_id": job_run_id,
        "message": f"已开始用已保存的来源证据重写《{label}》，完成后覆盖为新版本。",
    }


async def start_illustration_generation(
    settings: Settings,
    repository: ContentRepository,
    session: Any,
    *,
    session_id: str,
    draft: Any,
    purpose: str,
    placement: int,
    chat_run_id: str | None = None,
) -> dict[str, Any]:
    """配图公开入口：只入队，绝不同步等图片生成（那是对话“卡住”的根因）。

    对话派发与 Agent 工具共用；运行复用用户消息的受理运行，事件也写在它上面。
    """
    from app.tools.illustration_planner import style_for, subject_for

    resolved = purpose if purpose in {"cover", "inline"} else "cover"
    placement = 0 if resolved == "cover" else max(0, min(int(placement), MAX_PLACEMENT))
    chat_run = _task_run(
        repository, session_id, chat_run_id, ConversationIntent.GENERATE_DRAFT_IMAGE,
        auto_illustration=True,
    )
    _announce(
        repository, session_id, chat_run,
        f"好，正在为《{_draft_label(draft)}》生成{'封面图' if resolved == 'cover' else '正文插图'}。",
    )
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
    repository.update_image_generation_job(task.id, "queued", arq_job_id=arq_job_id)
    session.commit()
    logger.info(
        "start_illustration_generation session_id=%s draft_id=%s purpose=%s job_id=%s",
        session_id, draft.id, resolved, arq_job_id,
    )
    return {
        "status": "started",
        "status_text": "配图已入队",
        "draft_id": draft.id,
        "draft_title": _draft_label(draft),
        "purpose": resolved,
        "placement_after_paragraph": placement,
        "task_id": task.id,
        "chat_run_id": chat_run.id,
        "message": f"已开始为《{_draft_label(draft)}》生成{'封面图' if resolved == 'cover' else '正文插图'}。",
    }


async def _start_wechat_draft_delivery(
    settings: Settings,
    repository: ContentRepository,
    session: Any,
    *,
    session_id: str,
    draft: Any,
    chat_run_id: str | None = None,
    updating: bool = False,
) -> dict[str, Any]:
    """公众号草稿投递公开入口：入队后由后台完成并汇报（不会发表）。"""
    from app.jobs import enqueue_wechat_delivery_job

    chat_run = _task_run(
        repository, session_id, chat_run_id, ConversationIntent.PUBLISH_TO_WECHAT_DRAFT
    )
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
        job_id = await enqueue_wechat_delivery_job(settings, draft.id, chat_run.id)
    except Exception as exc:
        logger.warning("wechat_delivery_enqueue_failed draft_id=%s error_type=%s", draft.id, type(exc).__name__)
        return {"status": "failed", "draft_id": draft.id, "message": f"投递任务未能入队：{exc}"}
    repository.add_chat_agent_event(
        chat_run.id, "投递任务已创建", f"任务编号：{job_id}",
        metadata={"phase": "text", "state": "running", "job_id": job_id, "draft_ids": [draft.id]},
    )
    session.commit()
    logger.info("wechat_delivery_enqueued draft_id=%s updating=%s job_id=%s", draft.id, updating, job_id)
    return {
        "status": "started",
        "status_text": "投递已入队",
        "draft_id": draft.id,
        "draft_title": _draft_label(draft),
        "updated_remote": updating,
        "chat_run_id": chat_run.id,
        "message": f"已开始{'更新' if updating else '创建'}公众号草稿（{_draft_label(draft)}），完成后我会汇报；不会发表。",
    }


async def start_wechat_draft_delivery(
    settings: Settings, repository: ContentRepository, session_id: str, draft: Any, *,
    chat_run_id: str | None = None,
) -> dict[str, Any]:
    """对话派发用的投递入口：自己开会话，状态由后台投递任务汇报。"""
    if not settings.auto_wechat_draft_enabled:
        return {"status": "rejected", "message": "公众号草稿投递开关未启用（AUTO_WECHAT_DRAFT_ENABLED=false）。"}
    existing = repository.get_wechat_publication_for_draft(draft.id)
    updating = bool(existing is not None and existing.wechat_draft_media_id)
    return await _start_wechat_draft_delivery(
        settings, repository, repository.session, session_id=session_id, draft=draft,
        chat_run_id=chat_run_id, updating=updating,
    )


def build_draft_action_tools(session_id: str, *, chat_run_id: str | None = None):
    """构造只作用于本会话草稿的动作 Tool。

    `chat_run_id` 传入用户消息的受理运行时，工具不再各自新建运行与重复气泡。
    """

    async def _impl_run_auto_review(
        draft_id: str = "", deliver: bool = False, revise: bool = True,
    ) -> dict[str, Any]:
        """对当前文章发起自动审核（规则 + 模型审核）。

        `revise=false` 表示**只出意见、不改稿**（用户要“先看审核怎么说”时用这个）；
        默认 true 会在有可执行意见时按意见改稿一轮再复审。
        deliver=true 表示审核通过后创建公众号草稿箱记录（不会发表）；默认 false 仅审核。
        """
        settings = get_settings()
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=REVIEWABLE_STATUSES)
            if draft is None:
                return {"status": "rejected", "message": reason}
            result = await _start_review(
                settings, repository, session_id, draft, deliver=deliver,
                chat_run_id=chat_run_id, revise=revise,
            )
            session.commit()
        logger.info(
            "agent_tool_run_auto_review session_id=%s draft_id=%s deliver=%s revise=%s status=%s",
            session_id, draft_id or "-", deliver, revise, result.get("status"),
        )
        return result

    async def _impl_rewrite_draft(draft_id: str = "") -> dict[str, Any]:
        """用已保存的来源证据重写当前文案正文（不重新采集、不新建草稿），覆盖为新版本。"""
        settings = get_settings()
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=EDITABLE_STATUSES)
            if draft is None:
                return {"status": "rejected", "message": reason}
            result = await _start_rewrite(
                settings, repository, session_id, draft, chat_run_id=chat_run_id, source_mode="auto"
            )
            session.commit()
        logger.info("agent_tool_rewrite_draft session_id=%s draft_id=%s status=%s", session_id, draft_id or "-", result.get("status"))
        return result

    async def _impl_rebuild_draft_body(draft_id: str = "") -> dict[str, Any]:
        """只用已保存的来源正文重写正文：不联网重抓（拆分 rewrite_draft 后的“只重写”那一半）。"""
        settings = get_settings()
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=EDITABLE_STATUSES)
            if draft is None:
                return {"status": "rejected", "message": reason}
            result = await _start_rewrite(
                settings, repository, session_id, draft, chat_run_id=chat_run_id, source_mode="snapshot"
            )
            session.commit()
        logger.info(
            "agent_tool_regenerate_draft_body session_id=%s draft_id=%s status=%s",
            session_id, draft_id or "-", result.get("status"),
        )
        return result

    async def _impl_refresh_draft_source(draft_id: str = "") -> dict[str, Any]:
        """重新联网抓取这篇的来源正文并存成新的证据（**不改正文、不调内容模型**）。

        与 `rewrite_draft` 的分工：这一步只管“证据是不是最新的”，正文要不要跟着重写由
        `regenerate_draft_body` 决定。抓取是几秒级的 HTTP 调用，因此同步返回结果；
        抓不到时**旧快照原样保留**，不会把草稿的证据链弄丢。
        """
        from app.services.normalizer import normalize_item
        from app.services.source_refresh import fetch_live_source
        from app.services.source_snapshots import DraftSourceSnapshotStore, SourceSnapshotError

        settings = get_settings()
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=EDITABLE_STATUSES)
            if draft is None:
                return {"status": "rejected", "message": reason}
            source = draft.source_item
            before_chars = len((source.content or "").strip())
            raw = RawSourceItem(
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
            try:
                refreshed = await fetch_live_source(settings, raw)
            except Exception as exc:
                logger.warning(
                    "agent_tool_refresh_source_failed session_id=%s draft_id=%s error_type=%s",
                    session_id, draft.id, type(exc).__name__,
                )
                return {
                    "status": "failed",
                    "draft_id": draft.id,
                    "draft_title": _draft_label(draft),
                    "message": f"重新获取来源失败：{exc}。旧来源与草稿都保持原样，没有改任何正文。",
                }
            fetched_chars = len((refreshed.content or "").strip())
            # 图片链接必须在去标签前保留，否则刷新一次就把 README 里的图全丢了（normalizer 口径）。
            item = normalize_item(refreshed)
            repository.save_source(item)
            stored = refreshed.model_copy(update={
                "content": item.content,
                "metadata": {
                    **refreshed.metadata,
                    "content_origin": "github_readme",
                    "readme_fetch_status": "success",
                    "readme_refreshed_at": datetime.now(UTC).isoformat(),
                },
            })
            snapshots = DraftSourceSnapshotStore(settings, repository)
            try:
                snapshot_replaced = snapshots.replace_github_readme(draft.id, stored)
            except SourceSnapshotError as exc:
                session.rollback()
                logger.warning(
                    "agent_tool_refresh_source_snapshot_failed draft_id=%s error_type=%s",
                    draft.id, type(exc).__name__,
                )
                return {"status": "failed", "draft_id": draft.id, "message": f"来源已取回但快照保存失败：{exc}"}
            row, stale_delivery = repository.record_source_refresh(
                draft.id, source_item_id=source.id, chars=fetched_chars,
                reason=f"会话 Agent 刷新来源：{before_chars} → {fetched_chars} 字",
            )
            _task_run(repository, session_id, chat_run_id, ConversationIntent.REGENERATE_DRAFT)
            session.commit()
            label = _draft_label(row)
            version = row.version
        logger.info(
            "agent_tool_refresh_draft_source session_id=%s draft_id=%s before=%s after=%s snapshot=%s",
            session_id, draft_id or "-", before_chars, fetched_chars, snapshot_replaced,
        )
        return {
            "status": "refreshed",
            "status_text": "来源已刷新",
            "draft_id": draft.id,
            "draft_title": label,
            "draft_version": version,
            "source_chars_before": before_chars,
            "source_chars_after": fetched_chars,
            "snapshot_stored": snapshot_replaced,
            "stale_delivery": stale_delivery,
            "body_changed": False,
            "message": (
                f"已重新获取《{label}》的来源正文（{before_chars} → {fetched_chars} 字）并存成新的证据；"
                "**正文没有改动**。要按新来源重写正文，请接着调用 regenerate_draft_body。"
                + ("原先的公众号草稿已标记为过期，重新投递会覆盖远端内容。" if stale_delivery else "")
            ),
        }

    async def _impl_list_active_tasks(draft_id: str = "") -> dict[str, Any]:
        """只读：这篇草稿现在有哪些任务在跑、哪些在排队。

        为什么需要它：重写要跑 9–12 分钟，用户在这期间说“再跑一轮审核”，
        若不先看队列就会**另起一个任务**去审即将被替换的旧版本（真实故障）。
        Agent 应当能先查这里，再决定是排队等待还是直接执行。
        """
        from app.services.pending_reviews import ACTIVE_IMAGE_STATUSES, read_pending_review

        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id)
            if draft is None:
                return {"status": "rejected", "message": reason}
            now = datetime.now(UTC)
            rewrite = repository.find_active_regeneration_run(
                draft.id,
                within_seconds=getattr(get_settings(), "collection_job_timeout_seconds", 900),
                exclude_run_id="",
            )
            review = repository.find_active_auto_review_run(draft.id)
            images = repository.count_active_image_jobs(draft.id, ACTIVE_IMAGE_STATUSES)
            pending = read_pending_review(repository, draft.id)
            label = _draft_label(draft)
            version = draft.version

        def minutes(run: Any) -> int:
            started = getattr(run, "attempt_started_at", None) or getattr(run, "created_at", None)
            if started is None:
                return 0
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            return max(0, int((now - started).total_seconds() // 60))

        active: list[dict] = []
        if rewrite is not None:
            active.append({"task": "重写正文", "started_minutes_ago": minutes(rewrite)})
        if review is not None:
            active.append({"task": "自动审核", "state": review.status})
        if images:
            active.append({"task": "生成配图", "count": images})
        summary = "；".join(
            f"{item['task']}（已进行 {item['started_minutes_ago']} 分钟）" if "started_minutes_ago" in item
            else f"{item['task']}（{item.get('count') or item.get('state')}）"
            for item in active
        ) or "没有在跑的任务"
        return {
            "status": "ok",
            "draft_id": draft.id,
            "draft_title": label,
            "draft_version": version,
            "active_tasks": active,
            "queued_review": pending,
            "busy": bool(active),
            "message": (
                f"《{label}》（版本 {version}）当前：{summary}。"
                + (
                    "审核已排队，会在前面的任务结束后自动开始，不需要再发起一次。"
                    if pending
                    else ""
                )
            ),
        }

    async def _impl_search_web_evidence(
        draft_id: str = "", queries: list[str] | None = None,
    ) -> dict[str, Any]:
        """联网检索并把结果存成这篇草稿的**联网证据**（真的调用外部检索服务）。

        什么时候用：正文里有不解释就读不懂的外部名称、或用户明确要求“查一下再改”。
        结果会落进草稿证据（带联网标记），审核与改稿都能看到，因此不会把新事实判成“来源未出现”。
        """
        from app.services.evidence_search import search_and_store_evidence

        settings = get_settings()
        cleaned = [" ".join(str(item).split())[:300] for item in (queries or []) if str(item).strip()]
        if not cleaned:
            return {"status": "rejected", "message": "请给出要检索的关键词（最多 2 个）。"}
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=REVIEWABLE_STATUSES)
            if draft is None:
                return {"status": "rejected", "message": reason}
            result = await search_and_store_evidence(settings, repository, draft, cleaned)
            session.commit()
            label = _draft_label(draft)
        return {
            **result,
            "draft_id": draft.id,
            "draft_title": label,
            "message": (
                f"已为《{label}》联网检索「{'、'.join(cleaned)}」，带回 {result.get('entries', 0)} 条补充资料，"
                "已存成这篇的联网证据；审核与改稿都会看到。"
                if result.get("entries")
                else f"《{label}》这次联网检索没有取回可用资料。"
            ),
        }

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
            return await start_illustration_generation(
                settings, repository, session,
                session_id=session_id, draft=draft, purpose=resolved, placement=placement,
                chat_run_id=chat_run_id,
            )

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
            return await _start_wechat_draft_delivery(
                settings, repository, session,
                session_id=session_id, draft=draft, chat_run_id=chat_run_id, updating=updating,
            )

    async def _impl_reselect_publication_assets(
        draft_id: str = "", deliver: bool = True,
    ) -> dict[str, Any]:
        """作废当前投递素材选择并按最新口径重新选择（真实截图优先、AI 配图补足）。

        `deliver=False` 只重新选择并落库，**不创建或覆盖远端草稿**（用户想先看看选了哪几张）；
        默认 deliver=True：已投递的草稿会在重选后原地覆盖远端公众号草稿。
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
            chat_run = _task_run(
                repository, session_id, chat_run_id, ConversationIntent.RESELECT_PUBLICATION_ASSETS
            )
            if deliver:
                pending = (
                    f"好，正在重新为《{_draft_label(draft)}》选择投递配图（真实截图优先，AI 配图补足）"
                    + ("，完成后会覆盖公众号草稿内容。" if updating else "。")
                )
            else:
                pending = (
                    f"好，正在重新为《{_draft_label(draft)}》选择投递配图；"
                    "只更新选择，不会创建或覆盖远端草稿。"
                )
            _announce(repository, session_id, chat_run, pending)
            repository.add_chat_agent_event(
                chat_run.id, "重新选择配图已入队",
                "已作废旧的投递素材选择，将按当前草稿里的全部图片重新选择。",
                "running",
                metadata={
                    "phase": "image", "state": "queued", "draft_ids": [draft.id],
                    "reselection": True, "delivery": deliver,
                },
            )
            session.commit()
        try:
            from app.jobs import enqueue_wechat_delivery_job

            job_id = await enqueue_wechat_delivery_job(settings, draft.id, chat_run.id, deliver)
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
            "status_text": "正在重新选择配图",
            "draft_id": draft.id,
            "draft_title": _draft_label(draft),
            "previous_selection_invalidated": invalidated,
            "updated_remote": updating,
            "chat_run_id": chat_run.id,
            "message": f"已开始重新选择《{_draft_label(draft)}》的投递配图，完成后我会汇报。",
        }

    @tool("list_active_tasks")
    def list_active_tasks(draft_id: str = "") -> dict[str, Any]:
        """只读：查看当前文章**现在有哪些任务在跑、哪些在排队**。

        长任务（重写正文 9–12 分钟、自动审核几分钟、配图）期间要先查这里，再决定是
        排队等待还是直接执行——不看队列就会另起一个任务，去审即将被替换掉的旧版本。
        """
        return run_coroutine_sync(_impl_list_active_tasks(draft_id=draft_id))

    @tool("search_web_evidence")
    def search_web_evidence(draft_id: str = "", queries: list[str] | None = None) -> dict[str, Any]:
        """联网检索最多 2 个关键词，并把结果存成当前文章的**联网证据**（真调用外部检索服务）。

        用于正文里“不解释就读不懂”的外部名称，或用户明确说“先查一下再改”。
        结果会带联网标记落库，审核与改稿都能看到；只补证据，不改正文、不投递。
        """
        return run_coroutine_sync(
            _impl_search_web_evidence(draft_id=draft_id, queries=queries)
        )

    # 会话 Agent 以同步方式执行工具：异步实现必须配同步外壳，否则 LangChain 抛
    # NotImplementedError（真实故障：对话模型报“暂时不可用”，failure_stage=agent_invoke）。
    @tool("run_auto_review")
    def run_auto_review(draft_id: str = "", deliver: bool = False) -> dict[str, Any]:
        """对当前文章发起自动审核，并按审核意见改稿一轮（需要“只看意见”用 review_draft）。

        deliver=true 表示审核通过后创建公众号草稿箱记录（不会发表）；默认 false 仅审核。
        """
        return run_coroutine_sync(_impl_run_auto_review(draft_id=draft_id, deliver=deliver))

    @tool("review_draft")
    def review_draft(draft_id: str = "") -> dict[str, Any]:
        """只审核当前文章、**不改稿**：返回评分与意见，正文保持原样。

        用户说“先看看审核怎么说”“只审不改”“别动文章”时用这个；审核意见可以用
        read_latest_review 读回来，需要改稿再用 apply_revision_issues。
        """
        return run_coroutine_sync(
            _impl_run_auto_review(draft_id=draft_id, deliver=False, revise=False)
        )

    @tool("rewrite_draft")
    def rewrite_draft(draft_id: str = "") -> dict[str, Any]:
        """用已保存的来源证据重写当前文案正文（不重新采集、不新建草稿），覆盖为新版本。

        需要已保存快照时才能联网重抓；想“先刷新来源、再决定要不要重写”请用
        refresh_draft_source + regenerate_draft_body。
        """
        return run_coroutine_sync(_impl_rewrite_draft(draft_id=draft_id))

    @tool("refresh_draft_source")
    def refresh_draft_source(draft_id: str = "") -> dict[str, Any]:
        """重新联网抓取当前文章的来源正文并把新证据存好，**不改正文、不重写文章**。

        用户说“来源更新了”“重新抓一下 README”“先看看最新来源”时用这个；抓完再决定要不要
        用 regenerate_draft_body 重写正文。抓不到会返回 failed，旧来源与草稿保持不变。
        """
        return run_coroutine_sync(_impl_refresh_draft_source(draft_id=draft_id))

    @tool("regenerate_draft_body")
    def regenerate_draft_body(draft_id: str = "") -> dict[str, Any]:
        """**只用已保存的来源正文**重写当前文章正文，不联网重抓、不重新采集，覆盖为新版本。

        这是刷过来源（refresh_draft_source）之后重写正文的正确工具：证据就是你刚刷新的那一份，
        不会又抓到不同版本的 README。没有可用证据时会被拒绝，正文不会被改动。
        """
        return run_coroutine_sync(_impl_rebuild_draft_body(draft_id=draft_id))

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

    @tool("read_current_draft")
    def read_current_draft(draft_id: str = "", max_chars: int = 4000) -> dict[str, Any]:
        """读取当前文章的标题、版本、摘要与正文（最多 max_chars 字）。

        用于回答“这篇现在写了什么”“帮我按内容改一改”这类问题；只读，不改动任何内容。
        """
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id)
            if draft is None:
                return {"status": "rejected", "message": reason}
            limit = max(500, min(int(max_chars or 4000), 12000))
            body = draft.body or ""
            return {
                "status": "ok",
                "draft_id": draft.id,
                "draft_title": _draft_label(draft),
                "draft_status": draft.status,
                "version": draft.version,
                "summary_cn": draft.summary_cn,
                "paragraph_count": len([item for item in body.split("\n\n") if item.strip()]),
                "body": body[:limit],
                "truncated": len(body) > limit,
                "source_name": draft.source_name,
                "source_url": draft.source_url,
            }

    @tool("read_latest_review")
    def read_latest_review(draft_id: str = "") -> dict[str, Any]:
        """读取当前文章最近一次自动审核的评分与意见（没有则返回 empty）。

        这是“按上次的意见再改一遍”的依据；只读，不触发审核或改稿。
        """
        from app.tools.auto_revision import revision_issues

        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id)
            if draft is None:
                return {"status": "rejected", "message": reason}
            reviews = repository.list_auto_review_runs(draft.id)
            if not reviews:
                return {"status": "empty", "message": "这篇还没有自动审核记录。", "draft_title": _draft_label(draft)}
            latest = reviews[0]
            model_report = latest.model_report_json or {}
            return {
                "status": "ok",
                "review_id": latest.id,
                "review_status": latest.status,
                "score": model_report.get("score"),
                "summary": model_report.get("summary"),
                "issues": revision_issues(latest.rule_report_json or {}, model_report),
                "draft_title": _draft_label(draft),
                "draft_version": draft.version,
                "created_at": latest.created_at.isoformat() if latest.created_at else None,
            }

    async def _impl_apply_revision_issues(
        draft_id: str = "", review_id: str = "", extra_issues: list[str] | None = None,
    ) -> dict[str, Any]:
        """把审核意见**应用到草稿**（后台改稿一次内容模型调用）。

        这是“零件”：只负责“按这些意见改这一稿”，不替你决定该用哪次审核、要不要先重新审核。
        `extra_issues` 让 Agent 把用户临时提出的要求（例如“第 3 段那句重复删掉”）一起带进来。
        """
        from app.jobs import enqueue_draft_revision_job

        settings = get_settings()
        extra = [str(item)[:300] for item in (extra_issues or []) if str(item).strip()][:8]
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id, allowed=EDITABLE_STATUSES)
            if draft is None:
                return {"status": "rejected", "message": reason}
            # 正文正在被重写时不再并发改稿：两个写者会抢同一个草稿版本号，
            # 后完成的那次以唯一约束冲突失败（真实故障：用户看到“生成失败”而正文已改）。
            # 这个判断放在最前面：先于“有没有审核记录”，否则用户会拿到误导性的拒绝理由。
            active_rewrite = repository.find_active_regeneration_run(
                draft.id,
                within_seconds=getattr(settings, "collection_job_timeout_seconds", 900),
                exclude_run_id=chat_run_id or "",
            )
            if active_rewrite is not None:
                return {
                    "status": "rejected",
                    "draft_id": draft.id,
                    "draft_title": _draft_label(draft),
                    "message": (
                        f"《{_draft_label(draft)}》正在重写正文，现在改稿会和它抢同一个版本号。"
                        "等重写完成后我再按这些意见改；那时也可以直接说“按审核意见改”。"
                    ),
                }
            run = None
            if review_id:
                try:
                    run = repository.get_auto_review_run(review_id)
                except Exception:
                    return {"status": "rejected", "message": "找不到指定的审核记录。"}
                if run.draft_id != draft.id:
                    return {"status": "rejected", "message": "该审核记录不属于这篇文章。"}
            else:
                reviews = repository.list_auto_review_runs(draft.id)
                run = reviews[0] if reviews else None
            if run is None and not extra:
                return {
                    "status": "rejected",
                    "message": "这篇还没有自动审核记录；可以先运行审核，或直接在 extra_issues 里给出要改的点。",
                }
            chat_run = _task_run(repository, session_id, chat_run_id, ConversationIntent.REGENERATE_DRAFT)
            _announce(repository, session_id, chat_run, f"正在按审核意见重改《{_draft_label(draft)}》…")
            repository.add_chat_agent_event(
                chat_run.id, "按审核意见改稿已入队",
                f"审核记录 {run.id if run else '（无，使用临时意见）'}；"
                f"将按其中的意见改写正文并覆盖为新版本，不重新采集、不投递。",
                "running",
                metadata={
                    "phase": "text", "state": "queued", "draft_ids": [draft.id],
                    "review_id": run.id if run else None, "extra_issues": extra,
                },
            )
            session.commit()
        try:
            job_id = await enqueue_draft_revision_job(
                settings, draft.id, run.id if run else "", chat_run.id, extra,
            )
        except Exception as exc:
            logger.warning("revision_enqueue_failed draft_id=%s error_type=%s", draft.id, type(exc).__name__)
            return {"status": "failed", "draft_id": draft.id, "message": f"改稿任务未能入队：{exc}"}
        with SessionLocal() as session:
            ContentRepository(session).add_chat_agent_event(
                chat_run.id, "改稿任务已创建", f"任务编号：{job_id}",
                metadata={"phase": "text", "state": "running", "job_id": job_id, "draft_ids": [draft.id]},
            )
            session.commit()
        logger.info(
            "agent_tool_apply_revision_issues draft_id=%s review_id=%s extra=%s job_id=%s",
            draft.id, run.id if run else "-", len(extra), job_id,
        )
        return {
            "status": "started",
            "status_text": "按审核意见改稿中",
            "draft_id": draft.id,
            "draft_title": _draft_label(draft),
            "review_id": run.id if run else None,
            "chat_run_id": chat_run.id,
            "message": f"已开始按审核意见重改《{_draft_label(draft)}》，完成后覆盖为新版本。",
        }

    @tool("apply_revision_issues")
    def apply_revision_issues(
        draft_id: str = "", review_id: str = "", extra_issues: list[str] | None = None,
    ) -> dict[str, Any]:
        """按审核意见改写当前文章正文，覆盖为新版本（不重新采集、不投递）。

        典型用法：先用 `read_latest_review` 看这次审核说了什么，再把要采纳的意见交给本工具；
        用户临时补充的要求放进 `extra_issues`。改稿是一次内容模型调用，会入队后台执行并在对话里汇报，
        因此调用后不要假设已经改完。
        """
        return run_coroutine_sync(
            _impl_apply_revision_issues(
                draft_id=draft_id, review_id=review_id, extra_issues=extra_issues
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

    @tool("plan_publication_assets")
    def plan_publication_assets(draft_id: str = "") -> dict[str, Any]:
        """只重新选择投递配图、**不投递**：把新的封面与正文插图选择落库，远端草稿保持不动。

        用户想先看看“会选哪几张”“换一套图再投”时用这个；确认后投递用 publish_to_wechat_draft。
        """
        return run_coroutine_sync(
            _impl_reselect_publication_assets(draft_id=draft_id, deliver=False)
        )

    return [
        run_auto_review,
        review_draft,
        rewrite_draft,
        refresh_draft_source,
        regenerate_draft_body,
        read_current_draft,
        read_latest_review,
        list_active_tasks,
        search_web_evidence,
        apply_revision_issues,
        plan_publication_assets,
        approve_draft,
        discard_draft,
        revoke_approval,
        generate_draft_illustration,
        publish_to_wechat_draft,
        reselect_publication_assets,
    ]
