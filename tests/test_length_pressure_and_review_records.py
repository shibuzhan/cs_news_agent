"""长度压力、评分刻度、审核记录标注与“仅根据意见改稿”。

真实复盘（2026-09-14）：同一篇稿子的审核分数是 72 → 88 → 74 → 0 → 58，用户问
“为什么改稿之后分数反而下降了？是不是规则不一致？”核查结论：

1. 74 → 58 之间**没有改稿**：中间两次都是 `source_regeneration`（按来源整篇重写换了一份文字）；
2. 分数下降是真的——机械指标显示第 4 版的 4-gram 重复占比是第 2 版的两倍；
3. 真因是**长度压力**：提示词只说“别写到接近上限”，模型就往上限堆，靠“换个说法重复”补字数；
4. 记录页只说“审核未通过 · 58 分”，看不出审的是哪一版、那一版怎么来的 → 只能误读成“越改越差”。

这个文件锁住四件事：提示词不再逼字数、评分有可复算的刻度、记录标清版本、改稿按钮的前置校验。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services.generator import _natural_article_instruction


def test_generation_prompt_defers_length_to_the_config_page() -> None:
    """长度口径已按用户要求改为“以配置页为准，且不因为字数重写”（见 test_length_policy.py）。"""
    instruction = _natural_article_instruction(1600, 2200)

    assert "这个区间来自配置页" in instruction
    assert "下限 1600 是硬性的" in instruction
    assert "不会因此重写" in instruction


def test_generation_prompt_forbids_rephrasing_the_same_point() -> None:
    instruction = _natural_article_instruction(1600, 2200)

    assert "同一件事全文只许说一次" in instruction
    assert "换一个说法把同一层意思再说一遍" in instruction


def test_revision_prompt_follows_the_configured_length_policy() -> None:
    from app.tools import auto_revision

    source = inspect.getsource(auto_revision.AutoRevisionTool.invoke)

    assert "配置页设置的区间" in source
    assert "是硬性的" in source
    # 字数不会触发重写；重复仍然禁止。
    assert "照常保存" in source
    assert "同一件事全文只许说一次" in source


def test_review_prompt_has_a_recomputable_score_scale() -> None:
    from app.tools import auto_review

    source = inspect.getsource(auto_review.AutoReviewTool.invoke)

    assert "扣分刻度" in source
    assert "每条 critical 扣 25 分" in source and "每条 major 扣 8 分" in source
    assert "同一处缺陷只扣一次" in source


def test_review_prompt_receives_the_previous_score_and_version() -> None:
    from app.tools import auto_review

    source = inspect.getsource(auto_review.AutoReviewTool.invoke)

    assert "_history_section" in source
    history = inspect.getsource(auto_review._history_section)
    assert "上一次审核" in history
    assert "请只评价**当前这一版**" in history


def test_review_record_records_the_reviewed_version_and_origin() -> None:
    from app.tools import auto_review

    source = inspect.getsource(auto_review.AutoReviewTool.invoke)

    assert '"reviewed_version"' in source
    assert '"version_origin"' in source
    assert "_version_origin" in source


def test_version_origin_distinguishes_rewrite_from_revision() -> None:
    from app.tools.auto_review import _version_origin

    assert _version_origin(SimpleNamespace(content_plan_json={"last_revision_reason": {"kind": "source_regeneration"}})) == "source_regeneration"
    assert _version_origin(SimpleNamespace(content_plan_json={"last_revision_reason": {"issues": ["重复"]}})) == "review_revision"
    assert _version_origin(SimpleNamespace(content_plan_json={})) == "initial"


def test_stores_write_the_revision_reason_for_the_ui() -> None:
    from app.storage.repositories import ContentRepository

    regenerate = inspect.getsource(ContentRepository.regenerate_draft)
    revision = inspect.getsource(ContentRepository.apply_auto_revision)

    assert 'row.content_plan_json["last_revision_reason"] = {"kind": "source_regeneration"}' in regenerate
    assert '"last_revision_reason": {"kind": "review_revision"' in revision


def test_review_list_annotates_version_origin_and_score_delta() -> None:
    from app.api import routes

    source = inspect.getsource(routes.annotate_auto_reviews)

    assert "score_delta" in source and "previous_score" in source
    assert "version_origin_label" in source
    assert "reviewed_current_version" in source


def test_revise_command_is_routed() -> None:
    from app.services.agent_commands import parse_agent_command

    parsed = parse_agent_command("按审核意见改稿｜draft=abc12345")

    assert parsed is not None and parsed.name == "apply_revision_issues"
    assert parsed.draft_id == "abc12345"


def test_revise_without_any_review_tells_the_user_the_next_step() -> None:
    """留一句可执行的下一步，而不是“没有审核记录”式拒绝。"""
    from app.agent_tools import draft_actions

    source = inspect.getsource(draft_actions.build_draft_action_tools)

    assert "先点“仅运行审核（只出意见，不改稿）”拿到意见" in source


def test_revise_refuses_a_stale_review() -> None:
    """审核意见属于更早的版本时不能直接改：否则会按旧意见改新稿。"""
    from app.agent_tools import draft_actions

    source = inspect.getsource(draft_actions.build_draft_action_tools)

    assert "version_mismatch" in source
    assert "版本对不上时按旧意见改稿可能改错地方" in source
    # 旧记录没有版本标记时只做近似判断（updated_at 会被元数据更新顶掉，不能当硬条件）。
    assert "approximate_stale" in source
