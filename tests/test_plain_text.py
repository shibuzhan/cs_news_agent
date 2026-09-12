from __future__ import annotations

from app.services.plain_text import (
    WECHAT_DESCRIPTION_MAX_CHARS,
    append_source_title,
    format_source_body,
    normalize_plain_text,
    normalize_wechat_description,
)


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


def test_github_body_removes_source_title_footer() -> None:
    body = format_source_body("正文说明\n\n原文标题：旧标题", "owner/repo", "github")

    assert body == "　　正文说明"
    assert "原文标题：" not in body
