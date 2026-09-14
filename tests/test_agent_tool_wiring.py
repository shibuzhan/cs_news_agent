"""Agent 工具挂载完整性：写了却忘记挂上等于没有这个能力。

真实发现：`build_publication_preference_tools`（8 个）与 `build_wechat_material_tools`（2 个）
在 `content_deep_agent.py` 里 import 了却没有加进 tools 列表；而界面按钮“排版偏好/素材库盘点”
解析出的正是这两组里的工具名 → 两条路径都找不到 → 退化成模型闲聊，用户以为“排版偏好不能改”。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import app.agents.content_deep_agent as deep
from app.agent_tools.conversation_context import build_conversation_context_tools
from app.agent_tools.draft_actions import build_draft_action_tools
from app.agent_tools.draft_assets import build_draft_asset_tools
from app.agent_tools.publication_preferences import build_publication_preference_tools
from app.agent_tools.source_media_tools import build_source_media_tools
from app.agent_tools.wechat_materials import build_wechat_material_tools


def test_deep_agent_mounts_all_six_tool_groups() -> None:
    source = inspect.getsource(deep.ContentDeepAgent._resolve_sync)

    for builder in (
        "build_conversation_context_tools",
        "build_draft_asset_tools",
        "build_draft_action_tools",
        "build_source_media_tools",
        "build_publication_preference_tools",
        "build_wechat_material_tools",
    ):
        assert f"{builder}(session_id" in source, f"{builder} 没有被挂到 DeepAgent 上"


def test_deep_agent_receives_the_preference_and_material_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """真正跑一次 Agent 装配（模型/检查点全部替换），断言工具真的进了 kwargs。"""
    captured: dict = {}

    class FakeAgent:
        def invoke(self, payload, config):  # noqa: ANN001, ARG002
            return {
                "structured_response": {
                    "intent": "general_chat",
                    "reply": "好的。",
                }
            }

    class Checkpointer:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def setup(self):
            return None

    class FakePostgresSaver:
        @classmethod
        def from_conn_string(cls, _connection_string):
            return Checkpointer()

    monkeypatch.setattr(deep, "_configure_profile", lambda: None)
    monkeypatch.setattr(deep, "ChatOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(deep, "PostgresSaver", FakePostgresSaver)
    monkeypatch.setattr(deep, "get_conversation_context_snapshot", lambda _session_id: {})
    monkeypatch.setattr(deep, "_agent_files", lambda: {})
    monkeypatch.setattr(deep, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or FakeAgent())

    settings = SimpleNamespace(
        database_url="postgresql+psycopg://user:pass@db:5432/news_agent",
        llm_model="test-model",
        openai_api_key="test-key",
        openai_base_url=None,
        request_timeout_seconds=20,
        conversation_agent_timeout_seconds=45,
        conversation_structured_output_mode="json",
        deep_agent_enabled=True,
        llm_enabled=True,
    )

    deep.ContentDeepAgent(settings)._resolve_sync("session-1", "排版偏好是什么", False)

    names = {item.name for item in captured["tools"]}
    expected = {
        item.name
        for item in build_conversation_context_tools("session-1")
        + build_draft_asset_tools("session-1")
        + build_draft_action_tools("session-1")
        + build_source_media_tools("session-1")
        + build_publication_preference_tools("session-1")
        + build_wechat_material_tools("session-1")
    }
    assert expected <= names, f"缺工具：{expected - names}"
    # 抽查两个最容易被漏掉的
    assert {"show_publication_preferences", "update_style_guide", "list_wechat_materials"} <= names
    # 草稿动作拆分出的“零件”也必须挂上：写了没挂 = 用户说“先刷新来源别重写”时 Agent 无工具可用。
    assert {"rewrite_draft", "refresh_draft_source", "regenerate_draft_body", "review_draft"} <= names


def test_button_commands_resolve_preference_and_material_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """界面按钮“排版偏好/素材库盘点”必须能确定性执行，而不是退化成模型闲聊。"""
    import asyncio

    from app.services import chat_dispatch
    from app.services.agent_commands import parse_agent_command
    from app.services.chat_dispatch import ChatDispatchContext, _dispatch_command

    calls: list[str] = []

    class FakeTool:
        def __init__(self, name: str) -> None:
            self.name = name

        async def ainvoke(self, arguments):  # noqa: ANN001, ARG002
            calls.append(self.name)
            return {"status": "ok", "message": f"{self.name} 已执行"}

    class FakeRepository:
        def add_chat_agent_event(self, *_args, **_kwargs) -> None:
            return None

        def get_chat_agent_run(self, _run_id):
            return SimpleNamespace(status="running", summary="", response_message_id="message-1")

    async def fake_compose(*_args, **_kwargs) -> str:
        return "已读取长期偏好。"

    monkeypatch.setattr(chat_dispatch, "build_draft_action_tools", lambda *_a, **_k: [])
    monkeypatch.setattr(
        chat_dispatch, "build_publication_preference_tools",
        lambda _sid: [FakeTool("show_publication_preferences")],
    )
    monkeypatch.setattr(
        chat_dispatch, "build_wechat_material_tools",
        lambda _sid: [FakeTool("list_wechat_materials")],
    )
    monkeypatch.setattr(chat_dispatch, "_compose_reply", fake_compose)

    async def run(command_text: str) -> None:
        ctx = ChatDispatchContext(
            settings=SimpleNamespace(),
            repository=FakeRepository(),
            session=SimpleNamespace(commit=lambda: None),
            session_id="session-1",
            run_id="run-1",
            content=command_text,
        )
        parsed = parse_agent_command(command_text)
        assert parsed is not None
        await _dispatch_command(ctx, parsed)

    asyncio.run(run("排版偏好"))
    asyncio.run(run("素材库盘点"))

    assert calls == ["show_publication_preferences", "list_wechat_materials"]
