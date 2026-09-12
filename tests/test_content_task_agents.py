from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.agents.content_task_agents as task_agents
from app.agents.content_task_agents import (
    ContentTaskAgentError,
    DraftWritingResponse,
    RestrictedContentTaskAgent,
    ReviewResponse,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        llm_enabled=True,
        content_llm_model="content-model",
        content_openai_api_key="content-key",
        content_openai_base_url="https://content.example/v1",
        review_llm_model="review-model",
        review_openai_api_key="review-key",
        review_openai_base_url="https://review.example/v1",
        llm_model=None,
        openai_api_key=None,
        openai_base_url=None,
        content_llm_timeout_seconds=90,
        content_llm_max_retries=0,
    )


def test_draft_task_agent_has_no_tools_memory_or_subagents(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        def invoke(self, payload):
            captured["payload"] = payload
            return {
                "structured_response": DraftWritingResponse(
                    title_options=["标题"],
                    summary_cn="摘要",
                    body="正文",
                )
            }

    monkeypatch.setattr(task_agents, "_configure_profile", lambda: None)
    monkeypatch.setattr(task_agents, "ChatOpenAI", lambda **kwargs: captured.setdefault("model", kwargs))
    monkeypatch.setattr(
        task_agents,
        "create_deep_agent",
        lambda **kwargs: (captured.setdefault("agent_kwargs", kwargs), FakeAgent())[1],
    )

    result = RestrictedContentTaskAgent(_settings(), "content").write("仅使用这些证据写作")

    assert result.title_options == ["标题"]
    assert captured["model"]["model"] == "content-model"  # type: ignore[index]
    assert captured["agent_kwargs"]["tools"] == []  # type: ignore[index]
    assert captured["agent_kwargs"]["skills"] == []  # type: ignore[index]
    assert captured["agent_kwargs"]["memory"] == []  # type: ignore[index]
    assert captured["agent_kwargs"]["subagents"] == []  # type: ignore[index]
    # tool 模式仍要求模型调用结构化工具提交结果。
    assert "必须调用 DraftWritingResponse 结构化工具" in captured["agent_kwargs"]["system_prompt"]  # type: ignore[index]
    assert captured["payload"] == {"messages": [("user", "仅使用这些证据写作")]}


def test_review_task_agent_uses_review_model_and_validates_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        def invoke(self, _payload):
            return {
                "structured_response": {
                    "score": 90,
                    "issues": [{"type": "措辞", "severity": "minor", "description": "可精简"}],
                    "summary": "总体可发布",
                }
            }

    monkeypatch.setattr(task_agents, "_configure_profile", lambda: None)
    monkeypatch.setattr(task_agents, "ChatOpenAI", lambda **kwargs: captured.setdefault("model", kwargs))
    monkeypatch.setattr(task_agents, "create_deep_agent", lambda **_kwargs: FakeAgent())

    result = RestrictedContentTaskAgent(_settings(), "review").review("审核这篇文章")

    assert isinstance(result, ReviewResponse)
    assert result.score == 90
    assert captured["model"]["model"] == "review-model"  # type: ignore[index]


def test_task_agent_rejects_missing_structured_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAgent:
        def invoke(self, _payload):
            return {"messages": []}

    monkeypatch.setattr(task_agents, "_configure_profile", lambda: None)
    monkeypatch.setattr(task_agents, "ChatOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(task_agents, "create_deep_agent", lambda **_kwargs: FakeAgent())

    with pytest.raises(ContentTaskAgentError, match="未返回结构化结果"):
        RestrictedContentTaskAgent(_settings(), "content").write("写作")


def _json_mode_settings() -> SimpleNamespace:
    """思考模式模型：只要求 JSON 文本，不发送 tool_choice。"""
    settings = _settings()
    settings.llm_structured_output_mode = "tool"
    settings.content_structured_output_mode = "json"
    settings.review_structured_output_mode = "json"
    return settings


def test_json_mode_content_agent_omits_tool_choice_and_validates_final_json(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        def invoke(self, payload):
            captured["payload"] = payload
            return {
                "messages": [
                    SimpleNamespace(
                        type="ai",
                        content='```json\n{"title_options": ["标题"], "summary_cn": "摘要", "body": "正文"}\n```',
                    )
                ]
            }

    monkeypatch.setattr(task_agents, "_configure_profile", lambda: None)
    monkeypatch.setattr(task_agents, "ChatOpenAI", lambda **kwargs: captured.setdefault("model", kwargs))
    monkeypatch.setattr(
        task_agents,
        "create_deep_agent",
        lambda **kwargs: (captured.setdefault("agent_kwargs", kwargs), FakeAgent())[1],
    )

    result = RestrictedContentTaskAgent(_json_mode_settings(), "content").write("仅使用这些证据写作")

    assert result.title_options == ["标题"]
    assert "response_format" not in captured["agent_kwargs"]  # type: ignore[operator]
    assert "JSON 文本模式" in captured["agent_kwargs"]["system_prompt"]  # type: ignore[index]
    # json 模式不得同时要求调用结构化工具，否则思考模式模型仍会被 tool_choice 拒绝。
    assert "必须调用 DraftWritingResponse 结构化工具" not in captured["agent_kwargs"]["system_prompt"]  # type: ignore[index]
    assert captured["agent_kwargs"]["tools"] == []  # type: ignore[index]


def test_json_mode_content_agent_rejects_non_json_final_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAgent:
        def invoke(self, _payload):
            return {"messages": [SimpleNamespace(type="ai", content="我会帮你写这篇文章。")]}

    monkeypatch.setattr(task_agents, "_configure_profile", lambda: None)
    monkeypatch.setattr(task_agents, "ChatOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(task_agents, "create_deep_agent", lambda **_kwargs: FakeAgent())

    with pytest.raises(ContentTaskAgentError, match="未返回可解析的 JSON 结构"):
        RestrictedContentTaskAgent(_json_mode_settings(), "content").write("写作")


def test_json_mode_review_agent_rejects_schema_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAgent:
        def invoke(self, _payload):
            return {"messages": [SimpleNamespace(type="ai", content='{"score": 500}')]}

    monkeypatch.setattr(task_agents, "_configure_profile", lambda: None)
    monkeypatch.setattr(task_agents, "ChatOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(task_agents, "create_deep_agent", lambda **_kwargs: FakeAgent())

    with pytest.raises(ContentTaskAgentError, match="返回结果不符合结构"):
        RestrictedContentTaskAgent(_json_mode_settings(), "review").review("审核这篇文章")
