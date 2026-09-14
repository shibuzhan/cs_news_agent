"""对话消息的统一派发：HTTP 只登记与回执，意图识别与业务执行都在 Worker 里完成。

真实反馈：“为什么直到任务结束才响应聊天”。此前 `POST /chat/sessions/{id}/messages`
在请求内同步跑模型意图识别，甚至同步等一张图片生成完；现在改为：

    HTTP：写用户消息 → 写确定性回执（不调模型）→ 建受理运行 → 入队 → 返回
    Worker：解析命令 / 识别意图 / 执行工具 / 逐步写事件 / 追加最终回复

同一条用户消息只对应**一条受理运行**：Agent 工具通过 `chat_run_id` 复用它，
不再各自新建运行和“好，正在…”的重复气泡（真实反馈：详情太啰嗦）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi.concurrency import run_in_threadpool

from app.agent_tools.draft_actions import (
    build_draft_action_tools,
    start_auto_review,
    start_illustration_generation,
)
from app.agents.chat_agent import ChatAgent
from app.agents.content_deep_agent import ContentDeepAgent, DeepAgentResolution
from app.config import Settings
from app.domain.models import (
    AttachmentStatus,
    ConversationIntent,
    ConversationRunStatus,
    RawSourceItem,
    SourceKind,
    normalize_github_target,
)
from app.jobs import enqueue_collection_job
from app.services.agent_commands import AgentCommand, parse_agent_command
from app.services.attachments import (
    AgentWorkspaceStore,
    AttachmentAccessError,
    AttachmentError,
    AttachmentLinkSigner,
    PrivateAttachmentStore,
    validate_image_attachment,
)
from app.services.chat_receipts import (
    STEP_COLLECT,
    STEP_DELIVER,
    STEP_IMAGE,
    STEP_LABELS,
    STEP_REVIEW,
    STEP_REWRITE,
    ChatPlan,
    plan_chat_message,
)
from app.services.generator import build_generator
from app.services.task_narration import compose_task_reply
from app.storage.repositories import ContentRepository, DraftNotFound
from app.storage.tables import AttachmentRow, DraftRow
from app.workflows.content_workflow import ContentPipeline


logger = logging.getLogger("news_agent.chat_dispatch")

# 受理运行的短状态：卡片标题直接显示它，所以必须是状态而不是一段说明。
STATUS_ACCEPTED = "正在处理"


class ChatDispatchError(RuntimeError):
    """派发过程中的可预期失败：消息直接给用户，不暴露内部细节。"""


@dataclass
class ChatDispatchOutcome:
    """一次派发的结果；`keep_running` 表示已有子任务接管这条运行的汇报。"""

    reply: str | None = None
    status: ConversationRunStatus = ConversationRunStatus.COMPLETED
    summary: str = "已完成处理"
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    keep_running: bool = False


@dataclass
class ChatDispatchContext:
    """派发所需的全部上下文；一条用户消息一个实例。"""

    settings: Settings
    repository: ContentRepository
    session: Any
    session_id: str
    run_id: str
    content: str
    attachment_id: str | None = None
    auto_review: bool = False
    auto_illustration: bool = False
    # 回复增量的发布器：有它时，最终回复边生成边推给前端（SSE），失败自动回退非流式。
    publisher: Any = None


def current_editable_draft(repository: ContentRepository, session_id: str) -> DraftRow | None:
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


async def bind_explicit_image_attachment(
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
        raise ChatDispatchError("当前附件不是可用图片，请选择 JPG、JPEG 或 PNG 图片")
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
        raise ChatDispatchError("图片附件暂时不可读取或保存") from exc
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


def _draft_label(draft: DraftRow) -> str:
    return (draft.title_options_json or ["当前草稿"])[0]


async def _compose_reply(
    ctx: ChatDispatchContext, *, task: str, facts: dict[str, Any], fallback: str
) -> str:
    """任务汇报（会调一次模型）；开启流式时边生成边推送，失败自动回退非流式。"""
    from app.services.task_narration import compose_task_reply, compose_task_reply_streaming

    if ctx.publisher is None:
        return await run_in_threadpool(
            compose_task_reply,
            ctx.settings,
            task=task,
            facts=facts,
            fallback=fallback,
            run_id=ctx.session_id,
        )
    return await run_in_threadpool(
        compose_task_reply_streaming,
        ctx.settings,
        task=task,
        facts=facts,
        fallback=fallback,
        run_id=ctx.session_id,
        on_delta=ctx.publisher.delta,
    )


# --------------------------------------------------------------------------------------
# 业务步骤：配图 / 审核 / 采集 / 投递 / 重写。都是“入队即返回”，绝不在请求或派发里同步等待。
# --------------------------------------------------------------------------------------


async def _step_image(ctx: ChatDispatchContext, plan: ChatPlan) -> dict[str, Any]:
    draft = current_editable_draft(ctx.repository, ctx.session_id)
    if draft is None:
        return {"status": "rejected", "message": "当前会话还没有可配图的文章：请先生成草稿，或明确指定要配图的文章。"}
    purpose = "inline" if plan.inline else "cover"
    return await start_illustration_generation(
        ctx.settings,
        ctx.repository,
        ctx.session,
        session_id=ctx.session_id,
        draft=draft,
        purpose=purpose,
        placement=plan.placement,
        chat_run_id=ctx.run_id,
    )


async def _step_review(ctx: ChatDispatchContext, *, deliver: bool = False) -> dict[str, Any]:
    draft = current_editable_draft(ctx.repository, ctx.session_id)
    if draft is None:
        return {"status": "rejected", "message": "当前会话还没有可审核的文章：请先生成草稿，或明确指定要审核的文章。"}
    return await start_auto_review(
        ctx.settings, ctx.repository, ctx.session_id, draft, deliver=deliver, chat_run_id=ctx.run_id
    )


async def _step_collect(
    ctx: ChatDispatchContext, *, target: str, sources: list[str], limit: int,
    auto_review: bool, auto_illustration: bool,
) -> dict[str, Any]:
    repository = ctx.repository
    anchor_message_id = repository.get_chat_agent_run(ctx.run_id).response_message_id
    repository.add_chat_agent_event(
        ctx.run_id, "加入后台采集队列",
        f"任务已入队；将只抓取指定项目 {target} 并生成待审核草稿，不读取榜单。"
        if target
        else "任务已入队；将按已登记来源规则生成待审核草稿。",
        metadata={
            "sources": sources,
            "limit": limit,
            "auto_illustration": auto_illustration,
            "target": target,
        },
    )
    repository.add_chat_agent_event(
        ctx.run_id, "文字生成中",
        f"正在获取 {target} 的仓库信息与 README，并生成待审核文案。"
        if target
        else "正在采集来源、提取证据并生成待审核文案。",
        "running",
        metadata={"phase": "text", "state": "running", "target": target},
    )
    ctx.session.commit()
    job_id = await enqueue_collection_job(
        ctx.settings, ctx.run_id, ctx.session_id, anchor_message_id, sources, limit,
        auto_review, auto_illustration, target,
    )
    repository.add_chat_agent_event(
        ctx.run_id, "后台任务已创建", f"任务编号：{job_id}", metadata={"job_id": job_id}
    )
    ctx.session.commit()
    logger.info(
        "chat_collection_enqueued run_id=%s sources=%s limit=%s job_id=%s",
        ctx.run_id, sources, limit, job_id,
    )
    return {
        "status": "started",
        "status_text": "采集已入队",
        "target": target,
        "limit": limit,
        "job_id": job_id,
        "message": f"已加入后台队列，正在按指定项目 {target} 获取资料并生成待审核草稿。"
        if target
        else "已加入后台队列，正在采集并生成待审核草稿。",
    }


async def _step_rewrite(ctx: ChatDispatchContext) -> dict[str, Any]:
    draft = current_editable_draft(ctx.repository, ctx.session_id)
    if draft is None:
        return {"status": "rejected", "message": "当前会话还没有可重写的文章。"}
    origin_run = ctx.repository.find_generation_run_for_draft(draft.id)
    if origin_run is None:
        return {"status": "rejected", "message": "当前草稿缺少可回溯的原生成记录，未创建新任务。"}
    from app.agent_tools.draft_actions import start_draft_rewrite

    return await start_draft_rewrite(
        ctx.settings, ctx.repository, ctx.session_id, draft, origin_run=origin_run, chat_run_id=ctx.run_id
    )


async def _step_deliver(ctx: ChatDispatchContext) -> dict[str, Any]:
    from app.agent_tools.draft_actions import start_wechat_draft_delivery

    draft = current_editable_draft(ctx.repository, ctx.session_id)
    if draft is None:
        return {"status": "rejected", "message": "当前会话还没有可投递的文章。"}
    return await start_wechat_draft_delivery(
        ctx.settings, ctx.repository, ctx.session_id, draft, chat_run_id=ctx.run_id
    )


def _step_outcome(ctx: ChatDispatchContext, step: str, facts: dict[str, Any]) -> ChatDispatchOutcome:
    """把一步的执行事实变成运行状态与（必要时）对话回复。

    入队成功的子任务接管这条运行：状态保持“处理中”，由子任务结束时追加汇报；
    被拒绝或已经有人在做的，则当场结束并给出短状态。
    """
    status = str(facts.get("status") or "")
    # started / queued_after_images 之后由子任务收尾；already_running 没有任何人会结束这条运行，
    # 所以它必须当场结束（否则对话里会永远挂着“处理中”）。
    accepted = status in {"started", "queued_after_images"}
    if accepted:
        return ChatDispatchOutcome(
            reply=None,
            summary=str(facts.get("status_text") or facts.get("message") or STATUS_ACCEPTED)[:60],
            results=[facts],
            keep_running=True,
        )
    return ChatDispatchOutcome(
        reply=str(facts.get("message") or "这一步没有执行。"),
        # “被拒绝”是正常回答（例如没有当前文章），不是故障：红色叉号只留给真正的错误。
        status=ConversationRunStatus.FAILED if status == "failed" else ConversationRunStatus.COMPLETED,
        summary=str(facts.get("message") or "未执行")[:60],
        results=[facts],
        error=str(facts.get("message") or "未执行") if status == "failed" else None,
    )


async def _dispatch_plan(ctx: ChatDispatchContext, plan: ChatPlan) -> ChatDispatchOutcome:
    """多步计划：按用户说的顺序执行（例如“先配图，然后自动审核”）。"""
    repository = ctx.repository
    plan_event = repository.add_chat_agent_event(
        ctx.run_id, "已理解多步指令", plan.event_detail(), "running",
        metadata={"phase": "plan", "state": "running", "steps": list(plan.steps)},
    )
    ctx.session.commit()
    if plan_event is None:
        return ChatDispatchOutcome(reply=None, keep_running=True, summary="运行已结束")

    facts: list[dict[str, Any]] = []
    replies: list[str] = []
    handoff = False
    review_folded = False
    for index, step in enumerate(plan.steps):
        if step == STEP_COLLECT:
            target = normalize_github_target(ctx.content) or ""
            # 采集流程自己就能在生成后接自动审核，避免计划里再排一次重复审核。
            review_folded = ctx.auto_review or STEP_REVIEW in plan.steps
            result = await _step_collect(
                ctx,
                target=target,
                sources=[SourceKind.GITHUB.value] if target else [],
                limit=5,
                auto_review=review_folded,
                auto_illustration=ctx.auto_illustration,
            )
        elif step == STEP_IMAGE:
            result = await _step_image(ctx, plan)
        elif step == STEP_REVIEW:
            if review_folded:
                facts.append({"step": step, "status": "folded_into_collection"})
                continue
            result = await _step_review(ctx)
        elif step == STEP_DELIVER:
            result = await _step_deliver(ctx)
        elif step == STEP_REWRITE:
            result = await _step_rewrite(ctx)
        else:  # pragma: no cover - 计划器只产生已知步骤
            continue
        facts.append({"step": step, **result})
        message = str(result.get("message") or "")
        executor = _step_outcome(ctx, step, result)
        if executor.keep_running:
            handoff = True
            # 配图之后的步骤还要继续（审核会自己排到配图完成）；其余子任务已经接管汇报。
            if step != STEP_IMAGE or index == len(plan.steps) - 1:
                break
            continue
        if message:
            replies.append(message)
    if handoff:
        return ChatDispatchOutcome(
            reply=None,
            # 卡片标题用中文步骤名，不要泄露内部代号（image/review）。
            summary="正在执行：" + " → ".join(STEP_LABELS[step] for step in plan.steps[:2]),
            results=facts,
            keep_running=True,
        )
    return ChatDispatchOutcome(
        # 多步都要如实回答：只报最后一步会让人以为第一步没跑（真实反馈：两条命令只回一条）。
        reply="\n".join(replies) if replies else "收到，已经按顺序处理完成。",
        summary="已完成：" + " → ".join(STEP_LABELS[step] for step in plan.steps),
        results=facts,
        error=None,
    )


# --------------------------------------------------------------------------------------
# 界面按钮命令（确定性解析，不消耗模型意图识别）
# --------------------------------------------------------------------------------------


async def _dispatch_command(ctx: ChatDispatchContext, parsed: AgentCommand) -> ChatDispatchOutcome | None:
    repository = ctx.repository
    if parsed.draft_id:
        # 界面命令里的 draft_id 是**用户点出来的**，不是模型猜的：如果这篇草稿本会话还没用过，
        # 先把它设为本会话当前文章再执行，而不是回一句“该草稿不属于本会话”（真实反馈：点了按钮却被拒）。
        try:
            draft = repository.get_draft(parsed.draft_id)
        except Exception:
            draft = None
        if draft is not None and not repository.session_references_draft(ctx.session_id, parsed.draft_id):
            repository.update_chat_session_memory(ctx.session_id, active_draft_id=draft.id)
            repository.add_chat_agent_event(
                ctx.run_id, "已把这篇设为本会话当前文章",
                f"《{(draft.title_options_json or ['当前草稿'])[0]}》来自界面命令，已切换到本会话继续处理。",
                metadata={"phase": "intake", "state": "running", "draft_ids": [draft.id], "adopted": True},
            )
            ctx.session.commit()
    tools = {item.name: item for item in build_draft_action_tools(ctx.session_id, chat_run_id=ctx.run_id)}
    if parsed.name in tools:
        arguments: dict[str, Any] = {}
        if parsed.draft_id:
            arguments["draft_id"] = parsed.draft_id
        if parsed.name == "run_auto_review":
            arguments["deliver"] = parsed.deliver
        if parsed.name == "generate_draft_illustration":
            arguments.update(purpose=parsed.purpose, placement_after_paragraph=parsed.placement)
        repository.add_chat_agent_event(
            ctx.run_id, "识别对话命令", f"已按命令执行：{parsed.label}",
            metadata={
                "phase": "text", "state": "running", "command": parsed.name,
                "draft_ids": [parsed.draft_id] if parsed.draft_id else [],
            },
        )
        ctx.session.commit()
        result = await tools[parsed.name].ainvoke(arguments)
        facts = {"command": parsed.name, "label": parsed.label, **(result or {})}
    elif parsed.name == "reuse_draft_assets":
        draft = current_editable_draft(repository, ctx.session_id) if not parsed.draft_id else repository.get_draft(parsed.draft_id)
        if draft is None:
            result = {"status": "rejected", "message": "没有找到要复用配图的草稿。"}
        else:
            illustrations = repository.list_draft_illustrations(draft.id)
            if not illustrations:
                result = {"status": "rejected", "message": "这篇目前没有可复用的配图；需要的话我可以按当前正文重新生成配图。"}
            else:
                cover = next((item for item in illustrations if item.purpose == "cover"), illustrations[0])
                inline = [item for item in illustrations if item.id != cover.id]
                repository.save_wechat_asset_selection(draft.id, cover.asset_id, [item.asset_id for item in inline])
                result = {
                    "status": "done",
                    "draft_id": draft.id,
                    "draft_title": _draft_label(draft),
                    "inline_count": len(inline),
                    "message": f"已复用当前草稿的配图：封面 1 张、正文插图 {len(inline)} 张。",
                }
        facts = {"command": parsed.name, "label": parsed.label, **result}
    else:  # pragma: no cover - 解析器不会产生未知命令
        return None

    result_status = str(facts.get("status") or "")
    if result_status in {"started", "queued_after_images"}:
        # 子任务接管汇报：受理运行保持“处理中”，结束时由子任务追加结果。
        return ChatDispatchOutcome(
            reply=None,
            summary=str(facts.get("message") or STATUS_ACCEPTED)[:60],
            results=[facts],
            keep_running=True,
        )

    reply = await _compose_reply(
        ctx,
        task=f"执行界面命令：{parsed.label}",
        facts=facts,
        fallback=str(facts.get("message") or "命令已执行。"),
    )
    repository.add_chat_agent_event(
        ctx.run_id, "已汇报执行结果", reply[:500],
        metadata={"phase": "text", "state": result_status or "done"},
    )
    logger.info(
        "chat_agent_command_executed session_id=%s command=%s status=%s",
        ctx.session_id, parsed.name, result_status,
    )
    return ChatDispatchOutcome(
        reply=reply,
        summary=str(facts.get("message") or "命令已执行")[:60],
        results=[facts],
    )


# --------------------------------------------------------------------------------------
# 附件提取（长流程，同样交给 Worker）
# --------------------------------------------------------------------------------------


async def _dispatch_attachment(ctx: ChatDispatchContext) -> ChatDispatchOutcome:
    settings = ctx.settings
    repository = ctx.repository
    attachment = repository.get_attachment(ctx.attachment_id or "")
    if attachment.session_id != ctx.session_id:
        return ChatDispatchOutcome(
            reply="附件不属于当前对话，未执行提取。",
            status=ConversationRunStatus.FAILED, summary="附件不属于当前对话",
            error="附件不属于当前对话",
        )
    anchor_message_id = repository.get_chat_agent_run(ctx.run_id).response_message_id
    if not settings.llm_enabled:
        return ChatDispatchOutcome(
            reply="已识别明确提取指令，但 LLM_ENABLED=false，附件没有被读取或处理。",
            status=ConversationRunStatus.FAILED, summary="模型未启用",
            error="LLM_ENABLED=false",
        )
    repository.add_chat_agent_event(
        ctx.run_id, "读取附件", "正在受控工作区内读取文本附件并提取正文。", "running",
        metadata={"phase": "text", "state": "running", "attachment_id": attachment.id},
    )
    processing = repository.create_attachment_processing(attachment.id, anchor_message_id)
    ctx.session.commit()
    try:
        from app.tools.files.workspace import SessionWorkspaceTool

        content = SessionWorkspaceTool(
            PrivateAttachmentStore(settings), AgentWorkspaceStore(settings)
        ).read_text(ctx.session_id, attachment).content[:30000]
        # 与附件下载接口同源的签名链接：来源快照里存的必须是可回溯的地址。
        download_url = (
            f"{settings.app_base_url}/api/attachments/{attachment.id}/download"
            f"?token={AttachmentLinkSigner(settings).issue(attachment.id)}"
        )
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
        result = ContentPipeline(ctx.session, build_generator(settings), settings).process([raw_item])
        source = repository.get_source_by_external_id(SourceKind.ATTACHMENT.value, attachment.id)
        draft = repository.get_draft_for_source(source.id) if source else None
        if result["errors"] or draft is None:
            error_message = "; ".join(error["error"] for error in result["errors"]) or "未生成待审核草稿"
            repository.finish_attachment_processing(
                processing.id, AttachmentStatus.FAILED, error_message=error_message
            )
            ctx.session.commit()
            return ChatDispatchOutcome(
                reply=f"附件提取失败：{error_message}",
                status=ConversationRunStatus.FAILED, summary="附件提取失败", error=error_message,
            )
        repository.finish_attachment_processing(processing.id, AttachmentStatus.PROCESSED, draft_id=draft.id)
        ctx.session.commit()
        return ChatDispatchOutcome(
            reply=f"已生成待审核草稿：《{_draft_label(draft)}》。可在生成记录中查看或配图。",
            summary="附件草稿已生成", results=[{"draft_id": draft.id}],
        )
    except Exception as exc:
        logger.exception("attachment_extraction_failed session_id=%s error_type=%s", ctx.session_id, type(exc).__name__)
        ctx.session.rollback()
        repository.finish_attachment_processing(processing.id, AttachmentStatus.FAILED, error_message=str(exc))
        ctx.session.commit()
        return ChatDispatchOutcome(
            reply=f"附件提取失败：{exc}",
            status=ConversationRunStatus.FAILED, summary="附件提取失败", error=str(exc),
        )


# --------------------------------------------------------------------------------------
# 模型决策分支（保留原有确定性兜底：模型只给意图、没调工具时由这里执行）
# --------------------------------------------------------------------------------------


async def _dispatch_decision(
    ctx: ChatDispatchContext, decision: Any, resolution: DeepAgentResolution
) -> ChatDispatchOutcome:
    repository = ctx.repository
    handled: frozenset[str] = getattr(resolution, "tools_called", frozenset()) or frozenset()
    if not resolution.success:
        return ChatDispatchOutcome(
            reply=decision.reply,
            status=ConversationRunStatus.FAILED,
            summary=str(decision.reply)[:60],
            error=decision.reply,
        )

    if decision.intent == ConversationIntent.COLLECT_NEWS:
        target = normalize_github_target(decision.target) or ""
        source_names = [source.value for source in decision.sources]
        if target and SourceKind.GITHUB.value not in source_names:
            # 点名了项目就必须包含 GitHub 来源，避免模型漏给 sources 时又去跑别的榜单。
            source_names.insert(0, SourceKind.GITHUB.value)
        auto_illustration = ctx.auto_illustration or decision.auto_illustration
        result = await _step_collect(
            ctx, target=target, sources=source_names, limit=decision.limit,
            auto_review=ctx.auto_review, auto_illustration=auto_illustration,
        )
        return ChatDispatchOutcome(
            reply=None, summary="正在采集", results=[result], keep_running=True,
        )

    if decision.intent == ConversationIntent.GENERATE_DRAFT_IMAGE:
        if "generate_draft_illustration" in handled:
            # Agent 已经调用工具入队（工具复用本运行），这里绝不重复生成第二张图。
            return ChatDispatchOutcome(
                reply=None, summary="正在生成配图",
                results=[{"tool": "generate_draft_illustration", "via": "agent_tool"}],
                keep_running=True,
            )
        plan = ChatPlan(
            steps=(STEP_IMAGE,),
            text=str(decision.reply),
            status="正在生成配图",
            intent=ConversationIntent.GENERATE_DRAFT_IMAGE,
            inline=decision.image_purpose == "inline",
            placement=decision.placement_after_paragraph,
        )
        result = await _step_image(ctx, plan)
        outcome = _step_outcome(ctx, STEP_IMAGE, result)
        if outcome.keep_running:
            return ChatDispatchOutcome(
                reply=None, summary=str(result.get("message") or "正在生成配图")[:60],
                results=[result], keep_running=True,
            )
        return ChatDispatchOutcome(
            reply=str(result.get("message") or decision.reply),
            status=outcome.status, summary=outcome.summary, results=[result], error=outcome.error,
        )

    if decision.intent == ConversationIntent.RUN_AUTO_REVIEW:
        if "run_auto_review" in handled:
            return ChatDispatchOutcome(
                reply=None, summary="正在审核",
                results=[{"tool": "run_auto_review", "via": "agent_tool"}], keep_running=True,
            )
        result = await _step_review(ctx, deliver=False)
        outcome = _step_outcome(ctx, STEP_REVIEW, result)
        if outcome.keep_running:
            return ChatDispatchOutcome(
                reply=None, summary=str(result.get("message") or "正在审核")[:60],
                results=[result], keep_running=True,
            )
        return ChatDispatchOutcome(
            reply=str(result.get("message") or decision.reply),
            status=outcome.status, summary=outcome.summary, results=[result], error=outcome.error,
        )

    if decision.intent == ConversationIntent.REUSE_DRAFT_ASSETS:
        return await _dispatch_command(ctx, AgentCommand(name="reuse_draft_assets", label="复用原配图"))

    if decision.intent == ConversationIntent.REGENERATE_DRAFT:
        draft = current_editable_draft(repository, ctx.session_id)
        if draft is None:
            return ChatDispatchOutcome(
                reply="当前会话还没有可重写的文章。", status=ConversationRunStatus.FAILED,
                summary="没有可重写的文章", error="没有当前文章",
            )
        result = await _step_rewrite(ctx)
        outcome = _step_outcome(ctx, STEP_REWRITE, result)
        if outcome.keep_running:
            return ChatDispatchOutcome(
                reply=None, summary=str(result.get("message") or "正在重写")[:60],
                results=[result], keep_running=True,
            )
        return ChatDispatchOutcome(
            reply=str(result.get("message") or decision.reply),
            status=outcome.status, summary=outcome.summary, results=[result], error=outcome.error,
        )

    if decision.intent == ConversationIntent.ATTACHMENT_DRAFT and ctx.attachment_id:
        return await _dispatch_attachment(ctx)

    # 普通对话：模型回复就是最终结果。
    return ChatDispatchOutcome(
        reply=decision.reply,
        summary=str(decision.reply)[:60],
    )


async def dispatch_chat_message(ctx: ChatDispatchContext) -> ChatDispatchOutcome:
    """Worker 侧的统一入口：一条用户消息 → 一次执行 → 一条结果。"""
    repository = ctx.repository
    run = repository.get_chat_agent_run(ctx.run_id)
    if run.status != ConversationRunStatus.RUNNING.value:
        logger.info("chat_dispatch_skipped run_id=%s status=%s", ctx.run_id, run.status)
        return ChatDispatchOutcome(reply=None, keep_running=True, summary=run.summary or "运行已结束")

    repository.add_chat_agent_event(
        ctx.run_id, "开始处理", "正在识别这条指令并执行；结果会追加到对话里。", "running",
        metadata={"phase": "intake", "state": "running", "intent": run.intent},
    )
    ctx.session.commit()

    attachment_requested = bool(
        ctx.attachment_id and ChatAgent().requests_attachment_extraction(ctx.content, ctx.attachment_id)
    )
    if attachment_requested:
        return await _dispatch_attachment(ctx)

    parsed = parse_agent_command(ctx.content)
    if parsed is not None:
        outcome = await _dispatch_command(ctx, parsed)
        if outcome is not None:
            return outcome

    plan = plan_chat_message(ctx.content, has_attachment=ctx.attachment_id is not None)
    if plan.multi and len(plan.steps) > 1:
        logger.info("chat_dispatch_plan session_id=%s steps=%s", ctx.session_id, plan.steps)
        return await _dispatch_plan(ctx, plan)

    from app.services.reply_stream import StreamPublisher, stream_enabled

    outcome: ChatDispatchOutcome | None = None
    publisher = StreamPublisher(ctx.run_id, "chat") if stream_enabled() else None
    ctx.publisher = publisher
    try:
        # 只有“看起来就是普通对话”的消息才把模型草稿实时推给用户：动作类意图的最终措辞由
        # 任务汇报负责，提前推送会出现“界面上有、历史里没有”的幽灵气泡。
        on_delta = publisher.delta if (publisher and plan.intent == ConversationIntent.GENERAL_CHAT) else None
        resolution = await ContentDeepAgent(ctx.settings).resolve(
            ctx.session_id, ctx.content, ctx.attachment_id is not None,
            chat_run_id=ctx.run_id, on_delta=on_delta,
        )
        decision = resolution.decision
        logger.info(
            "chat_deep_agent_resolved session_id=%s intent=%s attachment=%s success=%s failure_kind=%s tools=%s",
            ctx.session_id, decision.intent.value, ctx.attachment_id is not None,
            resolution.success, resolution.failure_kind, sorted(getattr(resolution, "tools_called", ()) or ()),
        )
        outcome = await _dispatch_decision(ctx, decision, resolution)
        return outcome
    finally:
        ctx.publisher = None
        if publisher is not None:
            if outcome is not None and outcome.reply and not outcome.keep_running:
                publisher.done(outcome.reply, status=outcome.status.value)
            else:
                # 没有可用的最终文本（转交子任务/失败前就已推送的草稿）：让前端丢弃临时气泡。
                publisher.done("", status="dropped")
