"""会话能力：素材增删改查工具、追问意图与审核证据对齐。"""

from __future__ import annotations

from app.domain.models import ConversationIntent


def test_asset_tools_are_registered_for_the_session() -> None:
    from app.agent_tools.draft_assets import build_draft_asset_tools

    tools = {item.name for item in build_draft_asset_tools("session-1")}

    assert tools == {
        "list_current_draft_illustrations",
        "set_current_draft_cover",
        "move_current_draft_illustration",
        "delete_current_draft_illustration",
        "attach_existing_asset_to_current_draft",
    }


def test_asset_tool_rejects_when_no_current_draft() -> None:
    """没有当前文章时必须明确拒绝，不能拿全局最新草稿顶上。"""
    from app.agent_tools import draft_assets

    class _Repository:
        def get_chat_session_memory(self, _session_id: str):
            return type("Memory", (), {"active_draft_id": None})()

    draft, reason = draft_assets._resolve_draft(_Repository(), "session-1")

    assert draft is None
    assert "当前文章" in reason


def test_follow_up_intents_exist() -> None:
    assert ConversationIntent.RUN_AUTO_REVIEW.value == "run_auto_review"
    assert ConversationIntent.REUSE_DRAFT_ASSETS.value == "reuse_draft_assets"


def test_conversation_prompt_documents_follow_up_intents() -> None:
    from types import SimpleNamespace

    from app.services.conversation import OpenAICompatibleConversationModel

    captured: dict = {}

    class _Client:
        class chat:  # noqa: N801 - 模拟 OpenAI SDK 结构
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    captured["prompt"] = kwargs["messages"][0]["content"]
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content='{"intent":"general_chat","reply":"好"}'))]
                    )

    decider = OpenAICompatibleConversationModel.__new__(OpenAICompatibleConversationModel)
    decider.client = _Client()
    decider.model = "model"
    decider.decide("审核", False, [])

    prompt = captured["prompt"]
    assert "run_auto_review" in prompt
    assert "reuse_draft_assets" in prompt
    assert "答应现在审核" in prompt


def test_deep_agent_prompt_documents_asset_tools() -> None:
    from app.agents.content_deep_agent import _SYSTEM_PROMPT

    assert "list_current_draft_illustrations" in _SYSTEM_PROMPT
    assert "set_current_draft_cover" in _SYSTEM_PROMPT
    assert "不会上传、发布或删除素材文件" in _SYSTEM_PROMPT
