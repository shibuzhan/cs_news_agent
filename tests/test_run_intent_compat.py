"""运行类型兼容：枚举与字符串都必须可用。

真实故障：Agent 工具里给 `create_chat_agent_run()` 传了字符串意图，而仓储内部取 `.value`，
抛 `AttributeError: 'str' object has no attribute 'value'`，在对话里表现为“模型暂时不可用”。
"""

from __future__ import annotations

import inspect

from app.domain.models import ConversationIntent
from app.storage.repositories import GENERATION_RECORD_INTENTS


def test_generation_queue_includes_delivery_runs() -> None:
    assert ConversationIntent.PUBLISH_TO_WECHAT_DRAFT.value in GENERATION_RECORD_INTENTS
    assert ConversationIntent.RUN_AUTO_REVIEW.value in GENERATION_RECORD_INTENTS


def test_create_chat_agent_run_accepts_enum_and_string() -> None:
    """两个入参形式都要支持：改回只认枚举会让 Agent 工具再次崩。"""
    from app.storage.repositories import ContentRepository

    source = inspect.getsource(ContentRepository.create_chat_agent_run)

    assert "isinstance(intent, ConversationIntent)" in source
    assert "intent_value" in source


def test_session_tools_create_runs_with_enum_intents() -> None:
    from app.agent_tools import draft_actions

    source = inspect.getsource(draft_actions)

    assert "ConversationIntent.RUN_AUTO_REVIEW" in source
    assert "ConversationIntent.GENERATE_DRAFT_IMAGE" in source
    assert "ConversationIntent.PUBLISH_TO_WECHAT_DRAFT" in source
    # 不得再出现裸字符串意图（这正是崩溃来源）。
    assert '"run_auto_review", False' not in source
    assert '"publish_to_wechat_draft", False' not in source
