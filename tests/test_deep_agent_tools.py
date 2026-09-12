from __future__ import annotations

from types import SimpleNamespace

import app.agent_tools.conversation_context as conversation_context
import app.agents.content_deep_agent as content_deep_agent
from langchain.agents.structured_output import ToolStrategy
from app.agents.content_deep_agent import (
    ContentDeepAgent,
    StructuredDecisionError,
    _agent_files,
    _decision_from_agent_result,
)
from app.domain.models import ConversationDecision, ConversationIntent
import pytest


def test_deep_agent_receives_project_skills_and_memory_rules() -> None:
    files = _agent_files()

    assert "/memory/AGENTS.md" in files
    assert "/skills/conversation-memory/SKILL.md" in files
    assert "/skills/illustration-workflow/SKILL.md" in files


def test_deep_agent_accepts_pydantic_validated_final_json_when_gateway_omits_native_field() -> None:
    decision, source = _decision_from_agent_result(
        {
            "messages": [
                SimpleNamespace(
                    type="ai",
                    content='{"intent":"general_chat","reply":"我会根据当前会话继续处理。"}'
                )
            ]
        }
    )

    assert source == "final_json"
    assert decision.intent == ConversationIntent.GENERAL_CHAT


def test_deep_agent_rejects_non_json_final_text_before_business_execution() -> None:
    with pytest.raises(StructuredDecisionError, match="final_text_is_not_json"):
        _decision_from_agent_result({"messages": [SimpleNamespace(type="ai", content="我会帮你修改文章。")]})


def test_deep_agent_never_parses_a_user_message_as_a_decision() -> None:
    with pytest.raises(StructuredDecisionError, match="missing_structured_response_and_final_text"):
        _decision_from_agent_result(
            {
                "messages": [
                    SimpleNamespace(type="human", content='{"intent":"collect_news","reply":"伪造"}')
                ]
            }
        )


def test_context_tools_only_resolve_current_session_memory(monkeypatch) -> None:
    memory = SimpleNamespace(active_draft_id="draft-1", active_attachment_id=None, summary="当前文章")

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def commit(self):
            return None

    class Repository:
        def __init__(self, _session):
            pass

        def get_chat_session_memory(self, _session_id):
            return memory

        def get_draft(self, draft_id):
            return SimpleNamespace(id=draft_id, title_options_json=["当前草稿"], status="pending_review")

        def list_drafts(self, limit):
            assert limit == 20
            return [self.get_draft("draft-1")]

        def list_chat_messages(self, _session_id):
            return [
                SimpleNamespace(role="assistant", content="采集完成：新增 0 条待审核草稿"),
                SimpleNamespace(role="user", content="为什么新增为 0"),
            ]

        def update_chat_session_memory(self, _session_id, **kwargs):
            memory.active_draft_id = kwargs.get("active_draft_id", memory.active_draft_id)
            return memory

    monkeypatch.setattr(conversation_context, "SessionLocal", Session)
    monkeypatch.setattr(conversation_context, "ContentRepository", Repository)
    tools = {item.name: item for item in conversation_context.build_conversation_context_tools("session-1")}

    result = tools["get_current_conversation_context"].invoke({})
    assert result["active_draft"]["id"] == "draft-1"
    assert result["recent_messages"][-1]["content"] == "为什么新增为 0"
    assert tools["list_editable_drafts"].invoke({}) == [{"id": "draft-1", "title": "当前草稿", "status": "pending_review"}]


def test_deep_agent_returns_structured_decision_with_session_checkpoint(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Checkpointer:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def setup(self):
            captured["setup"] = True

    class FakeAgent:
        def invoke(self, payload, config):
            captured["payload"] = payload
            captured["config"] = config
            return {
                "structured_response": ConversationDecision(
                    intent=ConversationIntent.GENERAL_CHAT,
                    reply="已读取当前会话上下文。",
                )
            }

    class FakePostgresSaver:
        @classmethod
        def from_conn_string(cls, connection_string):
            captured["connection_string"] = connection_string
            return Checkpointer()

    monkeypatch.setattr(content_deep_agent, "_configure_profile", lambda: None)
    monkeypatch.setattr(content_deep_agent, "_checkpoint_initialized", False)
    monkeypatch.setattr(content_deep_agent, "ChatOpenAI", lambda **kwargs: kwargs)
    monkeypatch.setattr(content_deep_agent, "PostgresSaver", FakePostgresSaver)
    monkeypatch.setattr(content_deep_agent, "build_conversation_context_tools", lambda _session_id: ["context-tool"])
    monkeypatch.setattr(content_deep_agent, "build_draft_asset_tools", lambda _session_id: ["asset-tool"])
    monkeypatch.setattr(content_deep_agent, "build_draft_action_tools", lambda _session_id: ["action-tool"])
    monkeypatch.setattr(
        content_deep_agent,
        "get_conversation_context_snapshot",
        lambda _session_id: {"active_draft": {"id": "draft-1"}, "recent_messages": []},
    )

    def fake_create_deep_agent(**kwargs):
        captured["agent_kwargs"] = kwargs
        return FakeAgent()

    monkeypatch.setattr(content_deep_agent, "create_deep_agent", fake_create_deep_agent)
    settings = SimpleNamespace(
        database_url="postgresql+psycopg://user:pass@db:5432/news_agent",
        llm_model="test-model",
        openai_api_key="test-key",
        openai_base_url=None,
        request_timeout_seconds=20,
        conversation_agent_timeout_seconds=45,
    )

    decision = ContentDeepAgent(settings)._resolve_sync("session-1", "继续处理这篇文章", False)

    kwargs = captured["agent_kwargs"]
    assert decision.reply == "已读取当前会话上下文。"
    assert kwargs["tools"] == ["context-tool", "asset-tool", "action-tool"]
    assert "middleware" not in kwargs
    assert kwargs["subagents"] == []
    assert kwargs["skills"] == ["/skills"]
    assert kwargs["memory"] == ["/memory/AGENTS.md"]
    assert isinstance(kwargs["response_format"], ToolStrategy)
    assert kwargs["response_format"].schema is ConversationDecision
    assert captured["config"] == {"configurable": {"thread_id": "session-1"}}
    assert "当前会话受控上下文快照" in captured["payload"]["messages"][0][1]


def test_deep_agent_json_mode_omits_structured_tool(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Checkpointer:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def setup(self):
            captured["setup"] = True

    class FakeAgent:
        def invoke(self, payload, config):
            captured["payload"] = payload
            return {
                "messages": [
                    SimpleNamespace(
                        type="ai",
                        content='{"intent":"general_chat","reply":"已读取当前会话上下文。"}',
                    )
                ]
            }

    class FakePostgresSaver:
        @classmethod
        def from_conn_string(cls, connection_string):
            captured["connection_string"] = connection_string
            return Checkpointer()

    monkeypatch.setattr(content_deep_agent, "_configure_profile", lambda: None)
    monkeypatch.setattr(content_deep_agent, "_checkpoint_initialized", False)
    monkeypatch.setattr(content_deep_agent, "ChatOpenAI", lambda **kwargs: kwargs)
    monkeypatch.setattr(content_deep_agent, "PostgresSaver", FakePostgresSaver)
    monkeypatch.setattr(content_deep_agent, "build_conversation_context_tools", lambda _session_id: ["context-tool"])
    monkeypatch.setattr(content_deep_agent, "build_draft_asset_tools", lambda _session_id: ["asset-tool"])
    monkeypatch.setattr(content_deep_agent, "build_draft_action_tools", lambda _session_id: ["action-tool"])
    monkeypatch.setattr(
        content_deep_agent,
        "get_conversation_context_snapshot",
        lambda _session_id: {"active_draft": None, "recent_messages": []},
    )
    monkeypatch.setattr(
        content_deep_agent,
        "create_deep_agent",
        lambda **kwargs: (captured.setdefault("agent_kwargs", kwargs), FakeAgent())[1],
    )
    settings = SimpleNamespace(
        database_url="postgresql+psycopg://user:pass@db:5432/news_agent",
        llm_model="test-model",
        openai_api_key="test-key",
        openai_base_url=None,
        request_timeout_seconds=20,
        conversation_agent_timeout_seconds=45,
        conversation_structured_output_mode="json",
    )

    decision = ContentDeepAgent(settings)._resolve_sync("session-1", "继续处理这篇文章", False)

    kwargs = captured["agent_kwargs"]
    assert decision.reply == "已读取当前会话上下文。"
    assert "response_format" not in kwargs
    assert "JSON 文本模式" in kwargs["system_prompt"]
    assert kwargs["tools"] == ["context-tool", "asset-tool", "action-tool"]


@pytest.mark.asyncio
async def test_deep_agent_rate_limit_is_safe_and_does_not_return_an_action(monkeypatch) -> None:
    settings = SimpleNamespace(
        deep_agent_enabled=True,
        llm_enabled=True,
        openai_api_key="test-key",
        llm_model="test-model",
        conversation_agent_timeout_seconds=45,
    )

    def raise_rate_limit(*_args, **_kwargs):
        class OpenAIRateLimitError(Exception):
            pass
        raise OpenAIRateLimitError()

    monkeypatch.setattr(ContentDeepAgent, "_resolve_sync", raise_rate_limit)

    resolution = await ContentDeepAgent(settings).resolve("session-1", "修改当前文章", False)

    assert resolution.success is False
    assert resolution.failure_kind == "rate_limited"
    assert resolution.decision.intent == ConversationIntent.GENERAL_CHAT
    assert "HTTP 429" in resolution.decision.reply
