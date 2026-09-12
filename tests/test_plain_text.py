from __future__ import annotations

from app.services.plain_text import (
    GITHUB_SOURCE_HINT,
    WECHAT_DESCRIPTION_MAX_CHARS,
    append_source_title,
    extract_name_queries,
    format_source_body,
    normalize_plain_text,
    normalize_wechat_description,
)


def test_extract_name_queries_picks_external_names_and_skips_common_words() -> None:
    body = (
        "　　affaan-m/ECC 把计划与复查装进 Claude Code、Codex 与 OpenCode。"
        "Claude Code 是目前配合最好的工具，README 与 GitHub 属于常见语汇不应作为检索词。"
    )

    queries = extract_name_queries(body, limit=2)

    # 取出现次数最高、且不是通用语汇的名称；最多 2 条。
    assert queries[0] == "Claude Code"
    assert len(queries) == 2
    assert "README" not in queries and "GitHub" not in queries


def test_select_evidence_text_prefers_features_over_install_and_channels() -> None:
    from app.services.plain_text import select_evidence_text

    readme = "\n\n".join(
        [
            "# Project\n\nLanguage: English | 中文 | 日本語\n\n> Official sources only. Install only from the npm package ecc-universal.",
            "## Installation\n\nnpm install ecc-universal\n\nyarn add ecc-universal\n\nNode.js 18 required.",
            "## Overview\n\n"
            + "This project is a workflow system for coding agents. " * 6,
            "## Features\n\n"
            + "It provides planning, review and memory features for everyday work. " * 6,
            "## Pricing\n\nPrivate repositories start at 19 USD per seat per month. Sponsor us.",
            "## Usage\n\n"
            + "You describe the task and the agent follows the same checklist every time. " * 6,
        ]
    )

    selected = select_evidence_text(readme, budget=1100)

    assert len(selected) < len(readme) and len(selected) <= 1100
    # 保留开头概览（含语言清单所在的段），但优先补入 Overview/Features 这类实质内容。
    assert "Language: English" in selected
    assert "workflow system for coding agents" in selected
    assert "planning, review and memory features" in selected
    assert "Private repositories start at 19 USD" not in selected


def test_source_content_keeps_line_breaks_when_normalized() -> None:
    """来源正文一旦被压成一行，标题结构与后续的证据挑选都无从谈起。"""
    from app.services.normalizer import clean_text

    raw = "# Title\n\n## Features\n\n- item one\n- item two\n\n\n\n## Install\n\nnpm install demo"

    kept = clean_text(raw, 5000, keep_newlines=True)

    assert "## Features" in kept and "\n" in kept
    assert "- item one\n- item two" in kept
    # 连续空行压缩为一段，行内多余空白折叠。
    assert "\n\n\n" not in kept
    # 标题与摘要仍然单行，避免影响既有字段语义。
    assert "\n" not in clean_text(raw, 500)


def test_split_markdown_sections_keeps_intro_and_section_bodies() -> None:
    from app.services.plain_text import split_markdown_sections

    text = "Intro paragraph.\n\n## Overview\n\nWhat it is.\n\n### Features\n\nWhat it does.\n"

    sections = split_markdown_sections(text)

    assert [section.title for section in sections] == ["", "Overview", "Features"]
    assert sections[0].body == "Intro paragraph."
    assert sections[1].level == 2 and sections[1].body == "What it is."
    assert sections[2].level == 3 and sections[2].body == "What it does."
    assert split_markdown_sections("no headings here") == []


def test_select_evidence_text_returns_short_content_unchanged() -> None:
    from app.services.plain_text import select_evidence_text

    short = "一段很短的来源正文。"

    assert select_evidence_text(short, budget=20000) == short
    assert select_evidence_text("", budget=100) == ""


def test_extract_name_queries_returns_empty_for_text_without_names() -> None:
    assert extract_name_queries("　　这段正文只有中文，没有任何外部名称。") == []
    assert extract_name_queries("") == []


def test_normalize_plain_text_removes_markup_but_keeps_readable_content() -> None:
    result = normalize_plain_text(
        "# 标题\n\n- **重点**：看[来源](https://example.com)\n![](https://example.com/a.png)"
    )

    assert result == "标题\n\n重点：看来源"


def test_wechat_description_is_one_line_and_within_limit() -> None:
    description = normalize_wechat_description("第一句\n" + "很长的摘要" * 50)

    assert "\n" not in description
    assert len(description) <= WECHAT_DESCRIPTION_MAX_CHARS


def test_source_title_is_preserved_once_without_a_raw_link() -> None:
    body = append_source_title(
        "正文说明\n原文标题：旧标题\n原文链接：https://old.example",
        "新标题",
    )

    assert body.count("原文标题：") == 1
    assert body.startswith("　　正文说明")
    assert "原文标题：新标题" in body
    assert "原文链接：" not in body


def test_github_body_replaces_source_title_with_read_original_hint() -> None:
    body = format_source_body("正文说明\n\n原文标题：旧标题", "owner/repo", "github")

    # 项目名已出现在标题，正文不再重复原文标题，改为一句指向公众号“阅读原文”的提示。
    assert body == f"　　正文说明\n\n{GITHUB_SOURCE_HINT}"
    assert "原文标题：" not in body
