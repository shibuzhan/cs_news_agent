from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.domain.models import (
    ConversationDecision,
    ConversationIntent,
    ConversationRunStatus,
)
from app.services.conversation import ConversationModel
from app.storage.repositories import ContentRepository
from app.tools.plan_tools import (
    CreatePublishPlanRequest,
    CreatePublishPlanTool,
    CreateSchedulePlanRequest,
    CreateSchedulePlanTool,
)

Collector = Callable[[list[str], int], Awaitable[dict[str, Any]]]
DeepReply = Callable[[str, str], Awaitable[str | None]]
logger = logging.getLogger("news_agent.conversation_agent")


@dataclass(frozen=True)
class ConversationOutcome:
    reply: str
    run_id: str
    response_message_id: str
    status: ConversationRunStatus
    summary: str
    tool_results: list[dict[str, Any]]


class ConversationAgent:
    """自然语言只能路由到登记意图，绝不执行任意 Tool 或真实发布。"""

    def __init__(
        self,
        repository: ContentRepository,
        model: ConversationModel,
        collector: Collector,
        deep_reply: DeepReply | None = None,
    ):
        self.repository = repository
        self.model = model
        self.collector = collector
        self.deep_reply = deep_reply
        self.schedule_tool = CreateSchedulePlanTool()
        self.publish_tool = CreatePublishPlanTool()

    async def run(
        self,
        session_id: str,
        request_message_id: str,
        content: str,
        has_attachment: bool,
        decision: ConversationDecision | None = None,
    ) -> ConversationOutcome:
        history = [
            {"role": message.role, "content": message.content[:1000]}
            for message in self.repository.list_chat_messages(session_id)[-8:]
        ]
        decision = decision or self.model.decide(content, has_attachment, history)
        logger.info(
            "conversation_run_started session_id=%s intent=%s attachment=%s",
            session_id,
            decision.intent.value,
            has_attachment,
        )
        run = self.repository.create_chat_agent_run(
            session_id, request_message_id, decision.intent
        )
        self.repository.add_chat_agent_event(
            run.id,
            "识别对话意图",
            f"已识别为：{self._intent_label(decision.intent)}",
            metadata={"intent": decision.intent.value},
        )
        try:
            reply, status, summary, tool_results = await self._execute(
                decision, content, session_id, run.id
            )
        except Exception as exc:
            logger.exception(
                "conversation_run_failed run_id=%s intent=%s error_type=%s",
                run.id,
                decision.intent.value,
                type(exc).__name__,
            )
            reply = "本次请求处理失败，请稍后重试。"
            status = ConversationRunStatus.FAILED
            summary = "处理失败"
            tool_results = []
            self.repository.add_chat_agent_event(run.id, "执行失败", str(exc), "failed")
        response_message = self.repository.create_chat_message(session_id, "assistant", reply)
        self.repository.finish_chat_agent_run(
            run.id,
            response_message.id,
            status,
            summary,
            tool_results,
            None if status != ConversationRunStatus.FAILED else reply,
        )
        logger.info(
            "conversation_run_finished run_id=%s status=%s tool_result_count=%s",
            run.id,
            status.value,
            len(tool_results),
        )
        return ConversationOutcome(
            reply,
            run.id,
            response_message.id,
            status,
            summary,
            tool_results,
        )

    async def _execute(
        self,
        decision: ConversationDecision,
        content: str,
        session_id: str,
        run_id: str,
    ) -> tuple[str, ConversationRunStatus, str, list[dict[str, Any]]]:
        if decision.intent == ConversationIntent.GENERAL_CHAT:
            self.repository.add_chat_agent_event(run_id, "生成对话回复", "未调用外部业务 Tool")
            reply = decision.reply
            if self.deep_reply is not None:
                deep_reply = await self.deep_reply(session_id, content)
                if deep_reply:
                    reply = deep_reply
            return reply, ConversationRunStatus.COMPLETED, "已完成通用对话", []

        if decision.intent == ConversationIntent.COLLECT_NEWS:
            requested_sources = [source.value for source in decision.sources]
            self.repository.add_chat_agent_event(
                run_id,
                "调用资讯采集 Tool",
                "来源：" + ("、".join(requested_sources) if requested_sources else "全部已登记来源"),
                metadata={"sources": requested_sources, "limit": decision.limit},
            )
            result = await self.collector(requested_sources, decision.limit)
            summary = f"采集完成：新增 {result.get('created', 0)} 条待审核草稿"
            self.repository.add_chat_agent_event(run_id, "汇总采集结果", summary, metadata=result)
            return summary, ConversationRunStatus.COMPLETED, summary, [result]

        if decision.intent == ConversationIntent.CREATE_SCHEDULE_PLAN:
            plan = self.schedule_tool.invoke(
                self.repository,
                CreateSchedulePlanRequest(
                    session_id=session_id,
                    run_id=run_id,
                    schedule_text=decision.schedule_text or content[:500],
                    task_summary=content,
                    sources=[source.value for source in decision.sources],
                ),
            )
            detail = "已创建待确认定时计划；确认前不会注册或执行任务。"
            self.repository.add_chat_agent_event(run_id, "创建定时计划 Tool", detail, metadata={"plan_id": plan.id})
            return (
                f"已创建待确认定时计划：{plan.id}。{detail}",
                ConversationRunStatus.WAITING_CONFIRMATION,
                "等待确认定时计划",
                [{"tool": "create_schedule_plan", "plan_id": plan.id}],
            )

        if decision.intent == ConversationIntent.CREATE_PUBLISH_PLAN:
            plan = self.publish_tool.invoke(
                self.repository,
                CreatePublishPlanRequest(
                    session_id=session_id,
                    run_id=run_id,
                    platform=decision.platform or "待指定平台",
                    request_summary=content,
                ),
            )
            detail = "已创建待确认发布计划；确认前不会连接账号或发布内容。"
            self.repository.add_chat_agent_event(run_id, "创建发布计划 Tool", detail, metadata={"plan_id": plan.id})
            return (
                f"已创建待确认发布计划：{plan.id}。{detail}",
                ConversationRunStatus.WAITING_CONFIRMATION,
                "等待确认发布计划",
                [{"tool": "create_publish_plan", "plan_id": plan.id}],
            )

        if decision.intent == ConversationIntent.GENERATE_DRAFT_IMAGE:
            self.repository.add_chat_agent_event(
                run_id, "图片生成请求", "已路由到受控草稿插图 Tool；仅在图片服务已配置时执行。"
            )
            return decision.reply, ConversationRunStatus.COMPLETED, "等待图片生成 Tool", []

        self.repository.add_chat_agent_event(
            run_id, "附件草稿处理", "已交由附件专用受控流程处理"
        )
        return decision.reply, ConversationRunStatus.COMPLETED, "已路由附件处理", []

    @staticmethod
    def _intent_label(intent: ConversationIntent) -> str:
        return {
            ConversationIntent.GENERAL_CHAT: "通用对话",
            ConversationIntent.COLLECT_NEWS: "采集资讯",
            ConversationIntent.CREATE_SCHEDULE_PLAN: "创建定时计划",
            ConversationIntent.CREATE_PUBLISH_PLAN: "创建发布计划",
            ConversationIntent.ATTACHMENT_DRAFT: "附件生成草稿",
            ConversationIntent.GENERATE_DRAFT_IMAGE: "生成草稿插图",
            ConversationIntent.REGENERATE_DRAFT: "重新生成当前草稿",
        }[intent]
