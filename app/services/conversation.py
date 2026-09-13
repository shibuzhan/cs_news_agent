from __future__ import annotations

import json
import logging
from typing import Protocol

from langsmith.wrappers import wrap_openai
from openai import OpenAI
from pydantic import ValidationError

from app.config import Settings, api_key_for, base_url_for, model_for
from app.domain.models import ConversationDecision, ConversationIntent, SourceKind
from app.observability import langsmith_enabled


logger = logging.getLogger("news_agent.conversation_model")


class ConversationModel(Protocol):
    def decide(
        self, content: str, has_attachment: bool, history: list[dict[str, str]]
    ) -> ConversationDecision: ...


class DeterministicConversationModel:
    """模型不可用时安全降级：不以关键词触发任何业务 Tool。"""

    def decide(
        self, content: str, has_attachment: bool, history: list[dict[str, str]]
    ) -> ConversationDecision:
        return ConversationDecision(
            intent=ConversationIntent.GENERAL_CHAT,
            reply="对话模型暂时不可用，未执行采集、配图、改稿或发布操作。请稍后重试。",
        )


class OpenAICompatibleConversationModel:
    def __init__(self, settings: Settings):
        from app.services.runtime_settings import load_runtime_settings
        settings = load_runtime_settings(settings)
        client = OpenAI(
            api_key=api_key_for(settings, "conversation"),
            base_url=base_url_for(settings, "conversation") or None,
            max_retries=0,
            timeout=settings.conversation_agent_timeout_seconds,
        )
        self.client = wrap_openai(client) if langsmith_enabled(settings) else client
        self.model = model_for(settings, "conversation")
        self.fallback = DeterministicConversationModel()

    def decide(
        self, content: str, has_attachment: bool, history: list[dict[str, str]]
    ) -> ConversationDecision:
        prompt = (
            "你是资讯运营 Agent 的受控对话意图识别器。返回 JSON：intent、reply、"
            "sources、limit、target、schedule_text、platform、auto_illustration、image_purpose、placement_after_paragraph。intent 只能是 general_chat、"
            "collect_news、create_schedule_plan、create_publish_plan、attachment_draft、generate_draft_image、regenerate_draft、"
            "run_auto_review、reuse_draft_assets。"
            "用户在回答追问时：答应现在审核（“要”“跑一下审核”“现在就审”）用 **run_auto_review**；"
            "同意复用/保留原有配图（“复用”“用原来的图”“不用重新生成”）用 **reuse_draft_assets**。"
            "只有用户明确要求采集时使用 collect_news；只有明确提及定时/每天/每周时创建"
            "定时计划；只有明确提及发布时创建发布计划。发布和定时计划仅是待确认计划，"
            "不要声称已执行。普通聊天直接用简洁中文回复，不能编造实时事实。"
            "sources 只能从 arxiv、github、hacker_news、rss 中选择，limit 为 1-20。"
            "**target 是用户点名的具体 GitHub 项目**：消息里出现“owner/repo”或仓库链接"
            "（例如“bilawalsidhu/gods-eye-view 生成文案”“https://github.com/psf/requests 写一篇”）时，"
            "必须把仓库写成 owner/repo 填进 target，并在 sources 里包含 github。"
            "填了 target 就会只抓这个仓库、不读 Trending 榜单；**绝不能用榜单结果替代用户点名的项目**。"
            "用户只是泛泛要热点/今日资讯/趋势时 target 必须留空。"
            "只有用户明确肯定地要求生成图片、封面或插图时使用 generate_draft_image；图片只私有保存，不上传或发表。"
            "出现不要、别、不再、取消、停止、不用等否定词时，绝不能选择 generate_draft_image 或 auto_illustration=true。"
            "用户要求重新获取、重写或按来源更新当前已选文章时使用 regenerate_draft；没有具体修改要求时，选择 general_chat 并追问。"
            "image_purpose 只能是 cover 或 inline，placement_after_paragraph 为 0-20；仅在肯定的图片请求中填写。"
            f"用户是否附带附件：{has_attachment}。最近对话："
            f"{json.dumps(history[-8:], ensure_ascii=False)}。用户消息：{content[:4000]}"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            payload = json.loads(response.choices[0].message.content or "{}")
            decision = ConversationDecision.model_validate(payload)
            if decision.intent == ConversationIntent.ATTACHMENT_DRAFT and not has_attachment:
                return self.fallback.decide(content, has_attachment, history)
            logger.info("conversation_intent_llm intent=%s", decision.intent.value)
            return decision
        except Exception as exc:
            logger.warning(
                "conversation_intent_llm_fallback error_type=%s", type(exc).__name__
            )
            return self.fallback.decide(content, has_attachment, history)


def _explicit_collection_decision(normalized: str) -> ConversationDecision | None:
    """将明确的来源采集命令优先映射到队列，避免模型误判后同步执行。"""
    if not any(action in normalized for action in ("采集", "抓取", "提取", "编写文案", "生成文案")):
        return None
    source_map = (
        (("arxiv", "archive"), SourceKind.ARXIV),
        (("github",), SourceKind.GITHUB),
        (("hackernews", "hn"), SourceKind.HACKER_NEWS),
        (("rss",), SourceKind.RSS),
    )
    for markers, source in source_map:
        if any(marker in normalized for marker in markers):
            return ConversationDecision(
                intent=ConversationIntent.COLLECT_NEWS,
                reply="已识别为明确来源采集请求，将加入后台队列生成待审核草稿。",
                sources=[source],
                limit=5,
            )
    return None


def build_conversation_model(settings: Settings) -> ConversationModel:
    if settings.llm_enabled and api_key_for(settings, "conversation") and model_for(settings, "conversation"):
        return OpenAICompatibleConversationModel(settings)
    return DeterministicConversationModel()
