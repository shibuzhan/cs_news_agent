"""无副作用的文案与审核 DeepAgent 子能力。

它们只把已整理的输入转为 Pydantic 结构化输出；草稿保存、审核阈值、
改稿、图片和公众号投递仍由调用方的确定性服务负责。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from deepagents import create_deep_agent
from deepagents.backends.state import StateBackend
from langchain.agents.structured_output import ToolStrategy
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.agents.content_deep_agent import _configure_profile
from app.config import (
    Settings,
    api_key_for,
    base_url_for,
    model_for,
    structured_output_mode_for,
)


logger = logging.getLogger("news_agent.content_task_agents")


class ContentTaskAgentError(RuntimeError):
    """受限子 Agent 未返回可校验结构时的安全错误。"""


class DraftWritingResponse(BaseModel):
    """文案子 Agent 只能返回正文候选，不含来源或外部操作字段。"""

    title_options: list[str] = Field(min_length=1, max_length=3)
    summary_cn: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1, max_length=10000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    card_script: list[str] = Field(default_factory=list, max_length=6)
    claim_citations: list[dict[str, Any]] = Field(default_factory=list)


class ReviewIssue(BaseModel):
    type: str = Field(min_length=1, max_length=120)
    severity: Literal["critical", "major", "minor"]
    description: str = Field(min_length=1, max_length=1000)


class ReviewResponse(BaseModel):
    """审核子 Agent 的完整、可审计结论。"""

    score: int = Field(ge=0, le=100)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=30)
    summary: str = Field(default="", max_length=2000)


_WRITER_SYSTEM_PROMPT = """你是资讯运营 Agent 内部的文案生成子 Agent。
你只能依据调用方提供的证据、写作规则和选题规划生成结构化草稿内容。不得访问网页、文件、数据库、账号、图片或任何外部工具；不得编造来源未支持的事实、数字、人物、时间或结论。不得执行、建议或声称执行保存草稿、生成图片、审核、投递或发布操作。"""

_REVIEW_SYSTEM_PROMPT = """你是资讯运营 Agent 内部的文案审核子 Agent。
你只能基于调用方给出的草稿和来源字段，一次性列出全部可观察缺陷并给出评分。不得访问网页、文件、数据库、账号、图片或任何外部工具；不得改写草稿、触发改稿、修改状态、生成图片、投递或发布。"""

# 两种模式必须互斥：思考模式模型拒绝 tool_choice=required，json 模式下不得再要求调用结构化工具。
_TOOL_OUTPUT_SUFFIX = (
    "最终必须调用 {schema} 结构化工具提交结果，不得输出普通文本、Markdown、代码围栏或思维过程。"
)
_JSON_OUTPUT_SUFFIX = (
    "本次调用使用 JSON 文本模式，不要调用 {schema} 或任何工具：只输出一个 JSON 对象，"
    "字段与 {schema} 的参数定义完全一致。不得输出 Markdown 代码围栏、前后解释或思维过程。"
    "服务端会用 Pydantic 严格校验；校验失败时不会执行任何操作，也不会生成替代内容。"
)


def _final_message_text(result: Any) -> str:
    """只读取最终助手消息的文本字段，不读取用户消息或中间步骤。"""
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "type", None)
        if role not in {"assistant", "ai"}:
            continue
        content = (
            message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        )
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            text = "".join(parts).strip()
            if text:
                return text
    return ""


def _structured_payload(result: Any, name: str) -> Any:
    """原生结构化字段优先；缺失时只接受最终助手消息中的完整 JSON 对象。"""
    structured = result.get("structured_response") if isinstance(result, dict) else None
    if structured is not None:
        return structured
    text = _final_message_text(result)
    if not text:
        raise ContentTaskAgentError(f"{name} 未返回结构化结果")
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ContentTaskAgentError(f"{name} 未返回可解析的 JSON 结构") from exc
    if not isinstance(payload, dict):
        raise ContentTaskAgentError(f"{name} 返回的 JSON 不是对象")
    return payload


class RestrictedContentTaskAgent:
    """每次调用临时创建一个任务子 Agent，不共享会话 checkpoint 或记忆。"""

    def __init__(
        self,
        settings: Settings,
        task: Literal["content", "review"],
        model_override: str | None = None,
    ):
        self.settings = settings
        self.task = task
        self.selected_model = model_override or model_for(settings, task)
        self.selected_api_key = api_key_for(settings, task)
        if not (settings.llm_enabled and self.selected_api_key and self.selected_model):
            raise ContentTaskAgentError(f"{task} 子 Agent 的模型未启用或配置不完整")

    def write(self, prompt: str) -> DraftWritingResponse:
        if self.task != "content":
            raise ContentTaskAgentError("审核子 Agent 不能生成文案")
        return self._invoke(
            prompt,
            DraftWritingResponse,
            _WRITER_SYSTEM_PROMPT,
            "news_draft_writer",
        )

    def review(self, prompt: str) -> ReviewResponse:
        if self.task != "review":
            raise ContentTaskAgentError("文案子 Agent 不能执行审核")
        return self._invoke(
            prompt,
            ReviewResponse,
            _REVIEW_SYSTEM_PROMPT,
            "news_draft_reviewer",
        )

    def _invoke(
        self,
        prompt: str,
        schema: type[BaseModel],
        system_prompt: str,
        name: str,
    ) -> Any:
        _configure_profile()
        mode = structured_output_mode_for(self.settings, self.task)
        model = ChatOpenAI(
            model=self.selected_model,
            api_key=self.selected_api_key,
            base_url=base_url_for(self.settings, self.task) or None,
            temperature=0.2 if self.task == "content" else 0,
            timeout=self.settings.content_llm_timeout_seconds,
            max_retries=self.settings.content_llm_max_retries,
        )
        logger.info(
            "content_task_agent_started task=%s model_category=%s model=%s agent=%s structured_output_mode=%s",
            self.task,
            self.task,
            self.selected_model,
            name,
            mode,
        )
        agent_kwargs: dict[str, Any] = {
            "model": model,
            "tools": [],
            "system_prompt": "{base}\n{suffix}".format(
                base=system_prompt,
                suffix=(_TOOL_OUTPUT_SUFFIX if mode == "tool" else _JSON_OUTPUT_SUFFIX).format(
                    schema=schema.__name__
                ),
            ),
            "skills": [],
            "memory": [],
            "backend": StateBackend(),
            "subagents": [],
            "name": name,
        }
        if mode == "tool":
            # 普通模型使用强制工具调用；思考模式模型会以 400 拒绝 tool_choice，改用 JSON 文本模式。
            agent_kwargs["response_format"] = ToolStrategy(schema)
        try:
            agent = create_deep_agent(**agent_kwargs)
            result = agent.invoke({"messages": [("user", prompt)]})
        except Exception as exc:
            logger.warning(
                "content_task_agent_failed task=%s model_category=%s model=%s agent=%s structured_output_mode=%s error_type=%s",
                self.task,
                self.task,
                self.selected_model,
                name,
                mode,
                type(exc).__name__,
            )
            raise
        try:
            validated = schema.model_validate(_structured_payload(result, name))
        except ContentTaskAgentError:
            raise
        except Exception as exc:
            raise ContentTaskAgentError(f"{name} 返回结果不符合结构") from exc
        logger.info(
            "content_task_agent_finished task=%s model_category=%s model=%s agent=%s structured_output_mode=%s",
            self.task,
            self.task,
            self.selected_model,
            name,
            mode,
        )
        return validated
