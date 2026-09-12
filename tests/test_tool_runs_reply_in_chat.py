"""Agent 工具发起的运行必须在对话里有回复。

真实反馈：“失败原因这段内容不是应该在聊天框里回复吗？”——工具创建的运行没有助手消息，
后台任务的汇报无处可写，只能留在运行摘要里。
"""

from __future__ import annotations

import inspect

from app.agent_tools import draft_actions


def test_agent_tools_attach_an_assistant_message_to_their_run() -> None:
    source = inspect.getsource(draft_actions)

    # 三个会创建后台运行的工具都必须挂一条助手消息。
    assert source.count("_announce(") >= 5  # 定义 + 审核 + 配图 + 投递 + 重选配图
    assert "run.response_message_id = message.id" in source


def test_announce_creates_assistant_message_and_links_run() -> None:
    calls: list[tuple] = []

    class _Repository:
        class session:  # noqa: N801 - 模拟 SQLAlchemy session
            @staticmethod
            def flush() -> None:
                return None

        @staticmethod
        def create_chat_message(session_id: str, role: str, content: str):
            calls.append((session_id, role, content))
            return type("M", (), {"id": "message-1"})()

    class _Run:
        response_message_id = None

    run = _Run()
    message_id = draft_actions._announce(_Repository(), "session-1", run, "正在投递…")

    assert message_id == "message-1"
    assert calls == [("session-1", "assistant", "正在投递…")]
    assert run.response_message_id == "message-1"
