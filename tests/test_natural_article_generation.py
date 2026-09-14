from __future__ import annotations

import os

import pytest

# 只测试正文规范化，不连接项目数据库或模型服务。
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services.plain_text import NaturalArticleError, append_source_title, compose_natural_article


def test_natural_body_is_accepted_without_body_sections() -> None:
    raw_paragraphs = [f"第 {index} 段自然正文，解释项目相关背景、技术细节和使用边界。" * 12 for index in range(1, 6)]
    body, shape = compose_natural_article("\n\n".join(raw_paragraphs))
    rendered = append_source_title(body, "owner/repo")

    assert shape["paragraph_count"] == 5
    assert rendered.startswith("　　")
    assert rendered.endswith("原文标题：owner/repo")


def test_natural_body_keeps_a_bounded_paragraph_shape() -> None:
    with pytest.raises(NaturalArticleError, match="4 到 8"):
        compose_natural_article("\n\n".join(["正文" * 20] * 3))


def test_natural_body_requires_at_least_the_configured_minimum() -> None:
    """默认下限是“目标下限的底线”（800）；配置页填的目标值由 band 推导硬区间。"""
    with pytest.raises(NaturalArticleError, match="至少需要 800"):
        compose_natural_article("\n\n".join(["短正文" * 20] * 4))

    with pytest.raises(NaturalArticleError, match="至少需要 1400"):
        compose_natural_article("\n\n".join(["短正文" * 20] * 4), min_chars=1400)
