"""界面按钮 → Agent 明确命令 → Agent 工具。"""

from __future__ import annotations

import pytest

from app.services.agent_commands import parse_agent_command


def test_review_command_carries_draft_and_delivery_flag() -> None:
    parsed = parse_agent_command("运行自动审核｜draft=6713431f-fc24-4161-9bda-37faa0083f9e")

    assert parsed is not None
    assert parsed.name == "run_auto_review"
    assert parsed.draft_id == "6713431f-fc24-4161-9bda-37faa0083f9e"
    assert parsed.deliver is False


def test_review_command_detects_delivery_request() -> None:
    parsed = parse_agent_command("自动审核并创建公众号草稿")

    assert parsed is not None and parsed.name == "run_auto_review"
    assert parsed.deliver is True


def test_rewrite_and_review_decision_commands() -> None:
    assert parse_agent_command("重写文案").name == "rewrite_draft"
    assert parse_agent_command("重写本次生成的文案").name == "rewrite_draft"
    assert parse_agent_command("审核通过").name == "approve_draft"
    assert parse_agent_command("撤销审核").name == "revoke_approval"
    assert parse_agent_command("废弃文案").name == "discard_draft"
    assert parse_agent_command("复用原配图").name == "reuse_draft_assets"
    assert parse_agent_command("重新投递｜draft=abc12345").name == "publish_to_wechat_draft"
    assert parse_agent_command("投递到公众号草稿").name == "publish_to_wechat_draft"
    assert parse_agent_command("重新选择配图｜draft=abc12345").name == "reselect_publication_assets"
    assert parse_agent_command("重选配图").name == "reselect_publication_assets"


def test_illustration_command_reads_purpose_and_paragraph() -> None:
    cover = parse_agent_command("生成封面图｜draft=abc12345")
    inline = parse_agent_command("生成正文第 3 段插图｜draft=abc12345")

    assert cover is not None and cover.purpose == "cover" and cover.placement == 0
    assert inline is not None and inline.purpose == "inline" and inline.placement == 3
    assert inline.draft_id == "abc12345"


def test_free_text_is_not_treated_as_a_command() -> None:
    """自然语言交给模型意图识别，不能被命令解析器截走。"""
    assert parse_agent_command("帮我看看这篇写得怎么样") is None
    assert parse_agent_command("") is None
    assert parse_agent_command("x" * 300) is None


def test_action_tools_are_registered_for_the_session() -> None:
    from app.agent_tools.draft_actions import build_draft_action_tools

    names = {item.name for item in build_draft_action_tools("session-1")}

    assert names == {
        "run_auto_review",
        "rewrite_draft",
        "approve_draft",
        "discard_draft",
        "revoke_approval",
        "generate_draft_illustration",
        "publish_to_wechat_draft",
        "reselect_publication_assets",
    }


def test_action_tools_reject_foreign_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """只能操作本会话正在使用的草稿：从未被本会话碰过的草稿仍然拒绝。"""
    from app.agent_tools import draft_actions

    class _Repository:
        def get_draft(self, draft_id: str):
            return type(
                "Draft",
                (),
                {"id": draft_id, "status": "pending_review", "title_options_json": ["另一篇稿子"]},
            )()

        def session_references_draft(self, _session_id: str, _draft_id: str) -> bool:
            return False

    draft, reason = draft_actions._resolve_draft(_Repository(), "session-1", "draft-1")

    assert draft is None
    assert "不属于本会话" in reason
    # 拒绝时也要说清是哪一篇（用户反馈：返回信息应该是稿件标题，不是草稿 id）。
    assert "另一篇稿子" in reason
