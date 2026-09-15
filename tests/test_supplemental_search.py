"""材料不足时联网补充（生成/重写路径）与“指代式复述”禁令。

真实链条（2026-09-15）：
- 该草稿只给了 **1277 字 README**，却要写 1600–2200 字；生成/重写路径**从不联网补充**
  （搜索只在“审核后自动改稿”那一步用），模型只能靠“先列举、再用『这个/这些』总结一遍”填充；
- 审核连着两轮报重复：50 分 → 60 分（改稿只删了被点名的句子，同一模板在别处继续存在）；
- 用户口径：“材料不足时调用搜索工具补充啊，搜索背景、应用什么的”。

这里锁住三件事：材料不足时确实会联网补一次、补来的资料进提示词并有使用边界、
“指代式复述”在生成与改稿提示词里都被点名禁止。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.domain.models import NormalizedItem, SourceKind
from app.services import generator as generator_module
from app.services.generator import (
    SEARCH_TRIGGER_RATIO,
    OpenAICompatibleDraftGenerator,
    _natural_article_instruction,
)
from app.services.search_evidence import format_search_evidence_section


def _item(content: str, title: str = "openai/plugins") -> NormalizedItem:
    return NormalizedItem(
        source_kind=SourceKind.GITHUB,
        external_id="openai/plugins",
        title=title,
        url="https://github.com/openai/plugins",
        published_at=datetime.now(UTC),
        summary="插件示例库",
        content=content,
        source_name="GitHub",
        content_hash="a" * 64,
        category="开源项目",
        category_confidence=0.9,
        hot_score=10.0,
        metrics={},
        metadata={"readme_fetch_status": "success", "content_origin": "github_readme"},
    )


class FakeSearchTool:
    enabled = True

    def __init__(self, entries: list[dict] | None = None, error: Exception | None = None) -> None:
        self.entries = entries or []
        self.error = error
        self.queries: list[list[str]] = []

    def search(self, queries: list[str]) -> list[dict]:
        self.queries.append(list(queries))
        if self.error is not None:
            raise self.error
        return self.entries


def _generator(monkeypatch: pytest.MonkeyPatch, tool: FakeSearchTool) -> OpenAICompatibleDraftGenerator:
    # 子 Agent 从 content_task_agents 自己的命名空间取这两个函数，必须在那里替换。
    import app.agents.content_task_agents as task_agents

    monkeypatch.setattr(generator_module, "api_key_for", lambda *_a, **_k: "test-key")
    monkeypatch.setattr(generator_module, "model_for", lambda *_a, **_k: "test-model")
    monkeypatch.setattr(task_agents, "api_key_for", lambda *_a, **_k: "test-key")
    monkeypatch.setattr(task_agents, "model_for", lambda *_a, **_k: "test-model")
    settings = SimpleNamespace(
        llm_enabled=True,
        llm_model="test-model",
        draft_body_min_chars=1600,
        draft_body_max_chars=2200,
        llm_evidence_max_chars=20000,
        exa_mcp_enabled=True,
        revision_search_enabled=True,
        content_llm_timeout_seconds=30,
        content_llm_max_retries=0,
        openai_base_url=None,
    )
    instance = OpenAICompatibleDraftGenerator(settings, search_tool=tool)
    return instance


def test_thin_evidence_triggers_one_supplemental_search(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = FakeSearchTool([{"id": "exa-search-1", "title": "Exa：openai/plugins", "content": "背景资料"}])
    instance = _generator(monkeypatch, tool)
    item = _item("　　" + "README 正文。" * 60)  # 明显短于目标中段

    found = instance._supplemental_search(item, item.content)

    assert found and found[0]["id"] == "exa-search-1"
    assert tool.queries, "应当真的发起检索"
    assert "openai/plugins" in tool.queries[0][0], "检索词应包含项目名"


def test_enough_evidence_does_not_call_search(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = FakeSearchTool([{"id": "exa-search-1", "title": "x", "content": "y"}])
    instance = _generator(monkeypatch, tool)
    long_content = "　　" + "很长的 README 正文。" * 400  # 超过目标中段

    assert instance._supplemental_search(_item(long_content), long_content) == []
    assert tool.queries == [], "材料够用就不要打扰外部服务"


def test_search_failure_does_not_break_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.search_tools import ExaMcpSearchError

    tool = FakeSearchTool(error=ExaMcpSearchError("不可用"))
    instance = _generator(monkeypatch, tool)

    assert instance._supplemental_search(_item("短文"), "短文") == []


def test_supplemental_evidence_enters_the_prompt_with_limits() -> None:
    section = format_search_evidence_section(
        [{"id": "exa-search-1", "title": "Exa：openai/plugins", "content": "OpenAI 的插件目录说明"}]
    )

    assert "联网补充资料" in section
    assert "OpenAI 的插件目录说明" in section
    assert "不得用于编造项目事实" in section
    assert format_search_evidence_section([]) == ""


def test_generation_prompt_uses_the_shared_search_section() -> None:
    source = inspect.getsource(OpenAICompatibleDraftGenerator.generate)

    assert "format_search_evidence_section(supplemental)" in source
    assert "_supplemental_search(item, evidence_text)" in source


def test_generation_and_revision_forbid_proxy_restatement() -> None:
    instruction = _natural_article_instruction(1600, 2200)

    assert "指代式复述" in instruction
    assert "这个路径把插件入口收进" in instruction, "要给出可直接照做的反例"

    from app.tools import auto_revision

    revision = inspect.getsource(auto_revision.AutoRevisionTool.invoke)
    assert "逐句清掉“指代式复述”" in revision
    # 只删被点名的句子不够：同类未点名的也要处理。
    assert "未点名句子" in revision
    # 放宽“保留原文”，允许真正的删句与段内改写。
    assert "允许删句、允许在同一段内改写" in revision


def test_search_evidence_section_is_shared_by_both_paths() -> None:
    from app.tools import auto_revision
    from app.services.search_evidence import SEARCH_EVIDENCE_RULES

    source = inspect.getsource(auto_revision._search_evidence_section)

    assert "format_search_evidence_section" in source
    assert SEARCH_EVIDENCE_RULES.startswith("联网补充资料")
