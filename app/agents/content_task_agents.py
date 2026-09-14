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
from pydantic import BaseModel, Field, field_validator

from app.agents.content_deep_agent import _configure_profile
from app.config import (
    Settings,
    api_key_for,
    base_url_for,
    model_for,
    structured_output_mode_for,
)
from app.services.model_errors import schema_error_fields


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

    # 模型偶尔把列表字段写成字符串（`card_script` 已实际出现过）。这里只做**格式兼容**：
    # 不新增、不改写任何内容，超出长度上限时按上限截断，避免整条草稿因格式失败。
    @field_validator("title_options", mode="before")
    @classmethod
    def _coerce_title_options(cls, value: object) -> object:
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: object) -> object:
        if isinstance(value, str):
            # 只按显式分隔符拆分：标签本身常含空格（如 “GitHub Trending”“coding agent”）。
            return [item.strip() for item in re.split(r"[,，、;；]+", value) if item.strip()][:10]
        return value

    @field_validator("card_script", mode="before")
    @classmethod
    def _coerce_card_script(cls, value: object) -> object:
        if isinstance(value, str):
            lines = [line.strip() for line in value.splitlines() if line.strip()]
            return (lines or [value.strip()])[:6]
        return value

    @field_validator("body", mode="before")
    @classmethod
    def _coerce_body(cls, value: object) -> object:
        # 兼容历史的 body_sections 形态：数组合并为纯文本段落。
        if isinstance(value, list):
            return "\n\n".join(str(item).strip() for item in value if str(item).strip())
        return value


class ReviewIssue(BaseModel):
    type: str = Field(min_length=1, max_length=120)
    severity: Literal["critical", "major", "minor"]
    description: str = Field(min_length=1, max_length=1000)


class ReviewResponse(BaseModel):
    """审核子 Agent 的完整、可审计结论。"""

    score: int = Field(ge=0, le=100)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=30)
    summary: str = Field(default="", max_length=2000)
    # 审核模型顺手指出的、需要联网补一句说明的外部名称；复用审核调用，不额外增加模型请求。
    search_queries: list[str] = Field(default_factory=list, max_length=2)

    @field_validator("search_queries", mode="before")
    @classmethod
    def _coerce_search_queries(cls, value: object) -> list[str]:
        """只保留 1 到 2 条短检索词：兼容字符串、去空、去重、截断超长项。"""
        if isinstance(value, str):
            raw: list[object] = [value]
        elif isinstance(value, list):
            raw = value
        else:
            return []
        queries: list[str] = []
        for item in raw:
            text = " ".join(str(item).split()) if item is not None else ""
            if not text or text in queries:
                continue
            queries.append(text[:80])
            if len(queries) == 2:
                break
        return queries


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


def _invoke_task_agent(
    agent: Any, payload: dict[str, Any], *, mode: str, on_delta=None
) -> dict[str, Any]:
    """执行任务子 Agent；需要增量时改用流式，失败时回退非流式（生成不能因为流式而失败）。

    `stream_mode=["messages", "values"]` 同时给出 token 分片与最终状态：前者用来推 `body`，
    后者仍是原先 `invoke` 的返回值，因此结构校验逻辑完全不变。
    """
    if on_delta is None:
        return agent.invoke(payload)
    from app.services.reply_stream import JsonFieldExtractor

    extractor = JsonFieldExtractor("body")
    tool_args = _TaskToolArgs()
    final_state: Any = None
    try:
        for stream_mode, chunk in agent.stream(payload, stream_mode=["messages", "values"]):
            if stream_mode == "messages":
                message = chunk[0] if isinstance(chunk, tuple) else chunk
                text = _chunk_content(message) if mode == "json" else tool_args.feed(message)
                for piece in extractor.feed(text):
                    on_delta(piece)
            elif stream_mode == "values":
                final_state = chunk
    except Exception as exc:  # noqa: BLE001 - 网关不支持流式/中途断开都退回非流式
        logger.warning(
            "content_task_agent_stream_failed mode=%s error_type=%s", mode, type(exc).__name__
        )
        return agent.invoke(payload)
    if not isinstance(final_state, dict):
        logger.warning("content_task_agent_stream_incomplete mode=%s", mode)
        return agent.invoke(payload)
    return final_state


class _TaskToolArgs:
    """累积结构化输出工具的参数分片（tool 模式下正文就在这些 JSON 参数里）。"""

    def __init__(self) -> None:
        self._names: dict[Any, str] = {}

    def feed(self, message: Any) -> str:
        chunks = getattr(message, "tool_call_chunks", None)
        if not chunks and isinstance(message, dict):
            chunks = message.get("tool_call_chunks")
        out: list[str] = []
        for chunk in chunks or []:
            name = chunk.get("name") if isinstance(chunk, dict) else getattr(chunk, "name", None)
            index = chunk.get("index") if isinstance(chunk, dict) else getattr(chunk, "index", None)
            if name:
                self._names[index] = name
            tool = self._names.get(index)
            if tool is None or not str(tool).startswith("DraftWritingResponse"):
                continue
            args = chunk.get("args") if isinstance(chunk, dict) else getattr(chunk, "args", None)
            if isinstance(args, str) and args:
                out.append(args)
        return "".join(out)


def _chunk_content(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


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
        # 正文是整个流程里最慢的一步（一次调用 9–12 分钟）：把 body 字段边写边推给前端，
        # 用户能看到“正在写正文”，而不是一个多小时的黑箱等待。
        from app.services.reply_stream import active_publisher

        publisher = active_publisher()
        if publisher is not None:
            # 一次采集可能连续生成多篇：新的一篇开始时清空前端上一段的临时文本。
            publisher.reset()
        return self._invoke(
            prompt,
            DraftWritingResponse,
            _WRITER_SYSTEM_PROMPT,
            "news_draft_writer",
            on_delta=None if publisher is None else publisher.delta,
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
        on_delta=None,
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
            result = _invoke_task_agent(agent, {"messages": [("user", prompt)]}, mode=mode, on_delta=on_delta)
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
            # 只在本地日志记录出错字段与错误类型，不记录模型返回内容。
            logger.warning(
                "content_task_agent_schema_invalid task=%s agent=%s structured_output_mode=%s error_type=%s fields=%s",
                self.task,
                name,
                mode,
                type(exc).__name__,
                schema_error_fields(exc),
            )
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
