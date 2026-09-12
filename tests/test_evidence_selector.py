"""证据取材：按标题分节 → 模型挑章节 → 服务端取原文，失败自动回退。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.evidence_selector as selector
from app.services.evidence_selector import (
    _assemble,
    build_evidence,
    build_section_prompt,
    parse_section_numbers,
)
from app.services.plain_text import split_markdown_sections


README = "\n\n".join(
    [
        "Language: English | 中文 | 日本語",
        "## Install\n\nnpm install demo-package\n\nNode.js 18 required.",
        "## Why it exists\n\n"
        + "Teams keep re-explaining the same workflow to their agents. " * 6,
        "## How it works\n\n"
        + "A gated plan, test, review loop with evidence at every step. " * 6,
        "## Pricing\n\nPrivate repositories start at 19 USD per seat per month.",
    ]
)


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "llm_enabled": True,
        "llm_model": "selector-model",
        "openai_api_key": "selector-key",
        "openai_base_url": "https://selector.example/v1",
        "evidence_selector_llm_model": None,
        "evidence_selector_openai_base_url": None,
        "evidence_selector_openai_api_key": None,
        "content_llm_max_retries": 0,
        "content_llm_timeout_seconds": 30,
        "langsmith_tracing": False,
        "langsmith_api_key": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _fake_client(payload: str, captured: dict | None = None, *, raises: bool = False):
    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            if raises:
                raise RuntimeError("selector down")
            if captured is not None:
                captured["prompt"] = kwargs["messages"][0]["content"]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])

    return FakeClient


def test_section_prompt_lists_titles_with_numbers_and_previews() -> None:
    sections = split_markdown_sections(README)

    prompt = build_section_prompt(sections, budget=1200)

    assert "只返回 JSON" in prompt
    assert "0. （开头，无标题）" in prompt
    assert "1. Install" in prompt
    assert "跳过安装步骤" in prompt
    assert "1200" in prompt


def test_parse_section_numbers_drops_invalid_entries() -> None:
    assert parse_section_numbers('{"sections":[2,0,2,9,-1,"x",true]}', 4) == [2, 0]
    assert parse_section_numbers("not json", 4) == []
    assert parse_section_numbers('{"sections":"nope"}', 4) == []
    assert parse_section_numbers('[1, 2]', 4) == [1, 2]


def test_build_evidence_uses_model_choice_and_skips_rejected_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    # 模型只挑“开头 + Why it exists + How it works”，跳过 Install 与 Pricing。
    monkeypatch.setattr(selector, "OpenAI", _fake_client('{"sections":[0,2,3]}', captured))

    evidence = build_evidence(_settings(), README, budget=900, source_ref="owner/repo")

    assert "Teams keep re-explaining" in evidence
    assert "A gated plan, test, review loop" in evidence
    assert "npm install demo-package" not in evidence
    assert "19 USD per seat" not in evidence
    assert len(evidence) <= 900
    assert "How it works" in captured["prompt"]


def test_build_evidence_falls_back_when_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selector, "OpenAI", _fake_client("{}", raises=True))

    evidence = build_evidence(_settings(), README, budget=900, source_ref="owner/repo")

    # 回退到关键词挑选：仍然命中实质内容，且绝不返回空。
    assert evidence
    assert "Teams keep re-explaining" in evidence or "A gated plan, test, review loop" in evidence


def test_build_evidence_falls_back_without_headings(monkeypatch: pytest.MonkeyPatch) -> None:
    flat = "这一整行没有标题结构。" * 200
    called = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            called["count"] += 1

    monkeypatch.setattr(selector, "OpenAI", FakeClient)

    evidence = build_evidence(_settings(), flat, budget=300, source_ref="flat")

    assert called["count"] == 0  # 没有标题时不调用模型
    assert len(evidence) <= 300


def test_build_evidence_returns_short_content_unchanged() -> None:
    assert build_evidence(_settings(), "很短", budget=500) == "很短"
    assert build_evidence(_settings(), "", budget=500) == ""


def test_assemble_trims_oversized_section_inside_its_budget() -> None:
    sections = split_markdown_sections(
        "## Overview\n\n" + "Overview sentence about the project. " * 40 + "\n\n## Tiny\n\nshort"
    )

    assembled = _assemble(sections, [0, 1], budget=400)

    assert len(assembled) <= 400
    assert "Overview sentence" in assembled
