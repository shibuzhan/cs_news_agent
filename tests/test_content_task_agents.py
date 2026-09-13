from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import app.agents.content_task_agents as task_agents
from app.agents.content_task_agents import (
    ContentTaskAgentError,
    DraftWritingResponse,
    RestrictedContentTaskAgent,
    ReviewResponse,
)


@pytest.fixture(autouse=True)
def _isolate_from_stored_model_profiles(monkeypatch):
    """这些用例覆盖“环境变量回退链”，必须屏蔽数据库里的模型档案。

    真实情况：运营者在系统设置里把环境变量导入为模型档案后，
    `model_for()` 优先返回档案里的模型，这些用例会因为读到真实配置而失败。
    """
    monkeypatch.setattr("app.config._stored_model_profile_value", lambda *args, **kwargs: None)


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


def _run_json_mode(payload_text: str, monkeypatch: pytest.MonkeyPatch):
    """用 JSON 文本替身跑一次文案子 Agent，隔离外部模型。"""

    class FakeAgent:
        def invoke(self, _payload):
            return {"messages": [SimpleNamespace(type="ai", content=payload_text)]}

    monkeypatch.setattr(task_agents, "_configure_profile", lambda: None)
    monkeypatch.setattr(task_agents, "ChatOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(task_agents, "create_deep_agent", lambda **_kwargs: FakeAgent())
    return RestrictedContentTaskAgent(_json_mode_settings(), "content").write("仅使用这些证据写作")


def test_json_mode_coerces_string_list_fields_without_changing_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实事故形态：模型把 card_script 写成字符串，其余字段正常。"""
    payload = json.dumps(
        {
            "title_options": "i-have-adhd：让编程助手先给行动和编号步骤",
            "summary_cn": "摘要",
            "body": "正文",
            "tags": "开源项目, GitHub Trending、开发者工具",
            "card_script": "i-have-adhd 让编程助手先给行动和编号步骤，减少铺垫。",
            "claim_citations": [],
        },
        ensure_ascii=False,
    )

    result = _run_json_mode(payload, monkeypatch)

    assert result.card_script == ["i-have-adhd 让编程助手先给行动和编号步骤，减少铺垫。"]
    assert result.title_options == ["i-have-adhd：让编程助手先给行动和编号步骤"]
    assert result.tags == ["开源项目", "GitHub Trending", "开发者工具"]


def test_json_mode_joins_legacy_body_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {"title_options": ["标题"], "summary_cn": "摘要", "body": ["第一段", "第二段"]},
        ensure_ascii=False,
    )

    result = _run_json_mode(payload, monkeypatch)

    assert result.body == "第一段\n\n第二段"


def test_json_mode_splits_multiline_card_script_and_caps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "title_options": ["标题"],
            "summary_cn": "摘要",
            "body": "正文",
            "card_script": "\n".join(f"卡片{index}" for index in range(1, 9)),
        },
        ensure_ascii=False,
    )

    result = _run_json_mode(payload, monkeypatch)

    assert result.card_script == [f"卡片{index}" for index in range(1, 7)]


def test_json_mode_logs_only_field_paths_when_schema_still_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    payload = json.dumps({"title_options": 5, "summary_cn": "摘要", "body": "正文"}, ensure_ascii=False)

    with caplog.at_level(logging.WARNING, logger="news_agent.content_task_agents"):
        with pytest.raises(ContentTaskAgentError, match="返回结果不符合结构"):
            _run_json_mode(payload, monkeypatch)

    messages = [record.getMessage() for record in caplog.records]
    assert any(item.startswith("content_task_agent_schema_invalid") for item in messages)
    joined = " ".join(messages)
    assert "title_options" in joined
    # 诊断日志只含字段路径与错误类型，不含模型返回内容。
    assert payload not in joined
