"""长度口径与失败文案：字数按配置页、不因为字数重写、失败原因说人话。

真实故障（2026-09-15 01:33，用户截图）：重写任务失败，卡片显示“内容模型输出不符合草稿结构，
请重试或检查模型配置”。核查结论：

- **证据包没问题**：README 快照 1277 字全部进了提示词（`build_evidence` 在 `len<=budget` 时原样返回，
  所以没有 `evidence_built` 日志），预算 20000 远未触发裁剪；
- 真实原因是长度：`NaturalArticleError: 正文至少需要 1400 个字符（当前 1313）`；
- 而“1400”来自上一版我为了治“往上限堆字数”加的“写到中段、宁短勿凑”，模型这次踩穿了下限；
- 同时 `agent_skills/*-content-writing/SKILL.md` 写死“至少 1200 字符”，与代码的 1400 不一致。

用户口径（2026-09-15）：**字数以配置页为准；但不要因为字数问题重写**（一次生成 9–12 分钟）。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services.generator import (
    _generation_invalid_reason,
    _hard_body_minimum,
    _natural_article_instruction,
)
from app.services.plain_text import MIN_HARD_BODY_CHARS, NaturalArticleError


def test_formation_floor_is_only_the_absolute_minimum() -> None:
    """成形阶段不再用配置下限当硬门槛：低于目标只是“偏短”，照常保存。"""
    assert _hard_body_minimum(None) == MIN_HARD_BODY_CHARS == 400


def test_generation_prompt_states_the_configured_band_and_no_rewrite() -> None:
    instruction = _natural_article_instruction(1600, 2200)

    assert "这个区间来自配置页" in instruction
    assert "下限 1600 是硬性的" in instruction
    # 不能再说“宁短勿凑/写到中段”——上一版这句话让模型写到了 1313 字（低于 1400）。
    assert "宁短勿凑" not in instruction
    assert "写到中段就好" not in instruction
    # 也不许为了凑字数重复。
    assert "不许为了凑字数重复已经说过的内容" in instruction


def test_revision_prompt_follows_the_same_rule() -> None:
    from app.tools import auto_revision

    source = inspect.getsource(auto_revision.AutoRevisionTool.invoke)

    assert "配置页设置的区间" in source
    assert "下限 {minimum_chars} 是硬性的" in source or "是硬性的" in source
    assert "不会触发重写" in source or "照常保存" in source
    assert "宁短勿凑" not in source


def test_revision_length_retry_only_triggers_below_the_absolute_floor() -> None:
    """改稿只在“短到不成文”时重试；为几十个字重跑一次模型不值得。"""
    from app.tools import auto_revision

    source = inspect.getsource(auto_revision.AutoRevisionTool.invoke)

    assert "hard_minimum = MIN_HARD_BODY_CHARS" in source


def test_length_is_a_hint_not_a_blocking_failure() -> None:
    from app.tools.auto_review import rule_review

    body = "\n\n".join([f"　　这是第 {index} 段正文，用来占位。" for index in range(1, 5)])
    draft = SimpleNamespace(
        body=body,
        source_url="https://example.com/source",
        source_name="Example",
        title_options_json=["标题"],
        tags_json=["标签"],
        source_item=SimpleNamespace(source_kind="github"),
        content_plan_json={},
        version=1,
        updated_at=None,
    )

    rules = rule_review(draft, 5000, 9000)

    assert rules["passed"] is True, rules["failures"]
    assert "低于配置下限" in rules["length_note"]
    assert "不要为了字数重复已说过的内容" in rules["length_note"]


def test_review_prompt_forbids_major_for_length() -> None:
    from app.tools import auto_review

    source = inspect.getsource(auto_review.AutoReviewTool.invoke)

    assert "长度只作提示、不作为缺陷" in source
    assert "不得因为长度给出 major/critical" in source


def test_failure_message_names_the_real_reason() -> None:
    short = _generation_invalid_reason(NaturalArticleError("正文至少需要 400 个字符（当前 1313）"))

    assert "模型写得太短" in short
    assert "已保留原稿、未覆盖" in short
    assert "不符合草稿结构" not in short

    paragraphs = _generation_invalid_reason(NaturalArticleError("正文应为 4 到 8 个自然段"))
    assert "段落结构" in paragraphs


def test_source_skills_defer_length_to_the_config_page() -> None:
    """Skill 里写死“至少 1200 字符”，而校验按 1400 —— 模型 1313 字正好落在缝里被拒。"""
    from pathlib import Path

    for name in (
        "arxiv-content-writing",
        "github-content-writing",
        "hacker-news-content-writing",
        "rss-content-writing",
    ):
        text = Path(f"agent_skills/{name}/SKILL.md").read_text(encoding="utf-8")
        assert "1200" not in text, f"{name} 仍写死字数"
        assert "配置页" in text, f"{name} 没有说明字数来自配置页"
