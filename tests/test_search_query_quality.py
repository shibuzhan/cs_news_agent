"""检索词必须是“具体检索对象”，不能是裸名称。

真实反馈（2026-09-15，用户看证据面板）：
- 证据里三条联网补充的检索词是 `Codex`（两条）和 `Web`（一条）；
- `Codex` 搜回来的是官网首页（`Title: Codex in ChatGPT | AI Coding Agents… | URL: https://openai.com/codex`），
  `Web` 搜回来的是维基百科 World Wide Web 条目 —— 都是噪声，对写作毫无帮助；
- 用户要求：“联网搜索内容应该为 codex 插件用法、插件开发方法这样的具体搜索对象，
  而不是直接搜索 codex，这样只会搜到官网页面”。

来源追查：`origin=revision_search`，检索词来自审核模型的 `search_queries`（裸名称），
兜底路径的 `extract_name_queries` 又挑出了正文里的通用词 `Web`（当时不在停用词表里）。
"""

from __future__ import annotations

import inspect

import pytest

from app.services.plain_text import (
    USAGE_QUERY_SUFFIX,
    enrich_search_query,
    extract_name_queries,
    is_generic_query,
    query_has_no_subject,
)


def test_bare_name_becomes_a_specific_query() -> None:
    query = enrich_search_query("Codex", subject="openai/plugins")

    assert query == f"Codex {USAGE_QUERY_SUFFIX}"
    assert "用法" in query and "开发" in query


def test_review_queries_are_also_completed() -> None:
    """审核模型给出的“名称 + 方面”也要补上方法与文档，避免只命中官网首页。"""
    query = enrich_search_query("Codex 插件", subject="openai/plugins")

    assert query.endswith(USAGE_QUERY_SUFFIX)
    assert query.startswith("Codex 插件")


def test_already_specific_query_is_left_alone() -> None:
    specific = "Codex 插件怎么写"

    assert enrich_search_query(specific, subject="openai/plugins") == specific


def test_generic_words_are_rejected() -> None:
    for value in ("Codex", "Web", "API", "插件", "the"):
        assert is_generic_query(value) is True, value
    assert is_generic_query("Codex 插件 用法") is False


def test_body_scan_no_longer_offers_generic_words() -> None:
    """正文里的 “Web” 这类通用词不能当兜底检索词（会搜到维基百科）。"""
    body = "　　Codex 插件可以接进 Web、macOS 与 Figma；openai/plugins 里有样例。"

    names = extract_name_queries(body, limit=3)

    assert "Web" not in names
    assert "Codex" in names or "openai/plugins" in names


def test_background_suffix_is_gone() -> None:
    """只剩“用法/开发/扩展/文档”一种后缀；背景问句式后缀已被证明只会搜到百科与官网。"""
    assert "用法" in USAGE_QUERY_SUFFIX and "开发" in USAGE_QUERY_SUFFIX
    assert "是什么" not in USAGE_QUERY_SUFFIX and "背景" not in USAGE_QUERY_SUFFIX

    from app.services import plain_text

    assert not hasattr(plain_text, "BACKGROUND_QUERY_SUFFIX")


def test_subjectless_queries_are_dropped() -> None:
    """`Web`、`OpenAI` 这类没有检索主体的词补后缀也是噪声，必须整条丢掉。"""
    for value in ("Web", "OpenAI", "插件", "API", "openai wechat"):
        assert query_has_no_subject(value) is True, value
    for value in ("Codex", "Codex 插件", "openai/plugins", "Claude Code"):
        assert query_has_no_subject(value) is False, value


def test_body_fallback_no_longer_proposes_vendor_names() -> None:
    """兜底检索词不能给出厂商名：`OpenAI 用法 开发 扩展 文档` 只会搜到公司官网。"""
    body = "　　Codex 由 OpenAI 推出，插件可以直接读 Web 上的资料。"

    names = extract_name_queries(body, limit=3)

    assert "OpenAI" not in names
    assert "Codex" in names


def test_revision_search_completes_the_queries() -> None:
    from app.services import auto_delivery

    source = inspect.getsource(auto_delivery._revision_search_evidence)

    assert "enrich_search_query" in source
    assert "query_has_no_subject" in source
    assert "只写名称本身" not in source


def test_review_prompt_demands_specific_queries() -> None:
    from app.tools import auto_review

    source = inspect.getsource(auto_review.AutoReviewTool.invoke)

    assert "检索词必须是具体检索对象" in source
    assert "禁止" in source and "Web" in source


def test_generation_search_uses_the_same_enrichment() -> None:
    from app.services.generator import OpenAICompatibleDraftGenerator

    source = inspect.getsource(OpenAICompatibleDraftGenerator._supplemental_search)

    assert "enrich_search_query" in source
