from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.models import ContentCategory, NormalizedItem, SourceKind
from types import SimpleNamespace

from app.services.generator import (
    DeterministicDraftGenerator,
    EnhancedDraftGenerator,
    GenerationError,
    ResilientDraftGenerator,
    _ensure_source_title_options,
    _source_writing_skill,
)
from app.agents.content_task_agents import DraftWritingResponse
from app.tools.scripts.registry import RegisteredScriptError, run_registered_script


def item() -> NormalizedItem:
    return NormalizedItem(
        source_kind=SourceKind.GITHUB,
        external_id="owner/repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        published_at=datetime.now(UTC),
        summary="一个用于测试的开源项目。",
        content="一个用于测试的开源项目。",
        source_name="GitHub Trending",
        content_hash="a" * 64,
        category=ContentCategory.OPEN_SOURCE,
    )


def test_deterministic_draft_keeps_auditable_generation_fields() -> None:
    draft = DeterministicDraftGenerator().generate(item())

    assert draft.generation_mode == "deterministic"
    assert draft.content_plan["angle"] == "来源事实速览"
    assert draft.claim_citations[0]["evidence_ids"] == ["source-1"]
    assert "原文标题：" not in draft.body
    assert "原文链接：" not in draft.body
    assert draft.body.startswith("　　")
    assert all("owner/repo" in title for title in draft.title_options)


def test_github_title_options_always_include_project_name() -> None:
    payload = {"title_options": ["一款值得关注的 AI 编程工具"]}

    _ensure_source_title_options(payload, item())

    assert payload["title_options"] == ["owner/repo｜一款值得关注的 AI 编程工具"]


def test_source_writing_skill_loads_its_routed_reference() -> None:
    instructions = _source_writing_skill(item())

    assert "GitHub 项目文案" in instructions
    assert "来源专用参考资料" in instructions
    assert "README 证据与补充规则" in instructions


def test_resilient_generator_reports_provider_timeout_without_a_fallback_draft() -> None:
    class TimeoutGenerator:
        def generate(self, _item):
            raise TimeoutError("provider timeout")

    # 可恢复的供应商故障必须显式失败，不生成确定性替代文案。
    with pytest.raises(GenerationError, match="内容模型请求超时"):
        ResilientDraftGenerator(TimeoutGenerator()).generate(item())


def test_enhanced_generator_uses_search_evidence_for_term_explanation() -> None:
    class FakeSearchTool:
        enabled = True

        async def search(self, queries: list[str]) -> list[dict]:
            assert queries == ["RCE 漏洞 通俗解释 官方资料"]
            return [{
                "id": "exa-search-1",
                "title": "Exa 联网检索：RCE",
                "url": "https://example.com/rce",
                "summary": "RCE 是远程代码执行。",
                "content": "RCE 是远程代码执行，攻击者可在目标设备执行代码。",
                "source_name": "Exa MCP",
            }]

    generator = EnhancedDraftGenerator.__new__(EnhancedDraftGenerator)
    generator.settings = SimpleNamespace(
        llm_fast_model=None,
        llm_reasoning_model=None,
        llm_evidence_max_chars=12000,
        draft_body_min_chars=900,
        draft_body_max_chars=1200,
    )
    generator.model = "test-model"
    generator.search_tool = FakeSearchTool()

    class FakeWriter:
        def write(self, prompt: str) -> DraftWritingResponse:
            assert "科技资讯写作者" in prompt
            return DraftWritingResponse(
                title_options=["Chromium 漏洞需尽快更新"],
                summary_cn="Chromium 出现已被利用的漏洞，普通用户现在该做什么？",
                body="\n\n".join([
                    "RCE（攻击者可在目标设备上远程执行代码）会带来安全风险。" * 12,
                    "该漏洞的利用过程与 Chromium 的沙箱边界有关，用户需要理解更新的必要性。" * 12,
                    "及时更新能够降低风险，但仍应结合设备环境和官方修复说明判断影响范围。" * 12,
                    "后续应持续关注修复进度和公开通报，避免把单一报告误解为全部风险。" * 12,
                ]),
                tags=["安全"],
                card_script=["风险提示"],
                claim_citations=[{"claim": "RCE 可远程执行代码", "evidence_ids": ["exa-search-1"]}],
            )

    generator.writer = FakeWriter()

    def fake_json(_model: str, prompt: str) -> dict:
        if "检索规划器" in prompt:
            return {"need_search": True, "queries": ["RCE 漏洞 通俗解释 官方资料"], "reason": "来源未解释 RCE"}
        if "选题编辑" in prompt:
            return {"audience": "科技读者", "angle": "风险说明", "outline": ["背景"], "risks": []}
        if "内容质检员" in prompt:
            return {"status": "manual_review", "score": 90, "risks": [], "suggestions": []}
        raise AssertionError("最终写作必须交给文案子 Agent")

    generator._json = fake_json  # type: ignore[method-assign]
    draft = generator.generate(item())

    assert draft.generation_mode == "enhanced"
    assert any(entry["id"] == "exa-search-1" for entry in draft.evidence_pack)
    assert "RCE（攻击者可在目标设备上远程执行代码）" in draft.body
    assert "原文标题：" not in draft.body
    assert "原文链接：" not in draft.body
    assert "\n" not in draft.summary_cn


def test_registered_script_rejects_unregistered_code() -> None:
    assert "文件大纲" in run_registered_script("extract_text_outline", "第一段\n第二段")
    try:
        run_registered_script("python -c dangerous", "x")
    except RegisteredScriptError:
        pass
    else:
        raise AssertionError("未登记脚本必须被拒绝")
