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


def test_source_refresh_and_snapshot_rewrite_commands() -> None:
    """来源拆分的两条命令要确定性命中，并且不能被配图启发式抢走。"""
    assert parse_agent_command("刷新来源").name == "refresh_draft_source"
    assert parse_agent_command("重新抓取来源｜draft=abc12345").name == "refresh_draft_source"
    # “重新生成正文”也以“生成”开头且含“正文”，曾经会落到 generate_draft_illustration。
    assert parse_agent_command("重新生成正文").name == "regenerate_draft_body"
    assert parse_agent_command("按已保存证据重写").name == "regenerate_draft_body"
    # 配图命令本身不受影响。
    assert parse_agent_command("生成正文第 3 段插图").name == "generate_draft_illustration"
    assert parse_agent_command("生成封面图").name == "generate_draft_illustration"


def test_review_only_command_never_revises_the_body() -> None:
    """“仅审核”必须落在只审不改的 review_draft 上。

    真实歧义：按钮写着“仅运行审核（改稿不投递）”，命令却解析成 run_auto_review，
    而它会在有可执行意见时**改稿一轮**——用户以为只是看看意见，正文却被动了。
    """
    assert parse_agent_command("仅审核").name == "review_draft"
    assert parse_agent_command("仅运行审核｜draft=abc12345").name == "review_draft"
    assert parse_agent_command("仅审核｜draft=abc12345").draft_id == "abc12345"
    # 会改稿的入口保持不变：默认自动审核与“重新审核”仍走 run_auto_review。
    assert parse_agent_command("自动审核").name == "run_auto_review"
    assert parse_agent_command("运行自动审核").name == "run_auto_review"
    assert parse_agent_command("重新审核").name == "run_auto_review"


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
        # 只审不改：用户要“先看看审核怎么说”时用这个。
        "review_draft",
        "rewrite_draft",
        # 拆分出的两半：只刷新来源（不动正文）／只用已存证据重写正文。
        "refresh_draft_source",
        "regenerate_draft_body",
        # 长任务串行化要用的只读零件：先看队列，再决定要不要新建任务。
        "list_active_tasks",
        # 联网补充证据（真调外部检索服务）。
        "search_web_evidence",
        # 只读零件：让 Agent 自己先看现状与历史意见，再决定动作。
        "read_current_draft",
        "read_latest_review",
        # 动作零件：只负责“按这些意见改这一稿”，不替 Agent 决定流程。
        "apply_revision_issues",
        # 只重选配图、不投递。
        "plan_publication_assets",
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
