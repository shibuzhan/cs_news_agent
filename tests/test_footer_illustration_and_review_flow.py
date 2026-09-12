"""文末提示、插图位置上限，以及“审核后至少改稿一轮 / 仅审核”的流程约束。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import app.services.auto_delivery as auto_delivery
import app.tools.illustration_planner as illustration_planner
from app.domain.models import SourceKind
from app.services.image_brief import load_brief, pick_object, pick_style
from app.services.plain_text import (
    GITHUB_SOURCE_HINT,
    format_source_body,
    is_source_footer_line,
)
from app.services.wechat_official import render_wechat_html
from app.tools.auto_review import rule_review
from app.tools.illustration_planner import IllustrationPlanner, fallback_plan


def _github_body(paragraphs: int = 8, repeat: int = 6) -> str:
    return "\n\n".join(
        "　　" + f"这是第 {index} 段正文，说明项目背景、实现方式、适用边界与后续观察。" * repeat
        for index in range(1, paragraphs + 1)
    )


def test_github_body_ends_with_the_read_original_hint() -> None:
    body = format_source_body("第一段\n\n第二段", "owner/repo", "github")

    assert body.endswith(f"{GITHUB_SOURCE_HINT}")
    assert body.endswith(f"\n\n{GITHUB_SOURCE_HINT}")
    assert "原文标题：" not in body


def test_non_github_body_keeps_the_source_title_footer() -> None:
    body = format_source_body("第一段\n\n第二段", "示例标题", "rss")

    assert body.endswith("原文标题：示例标题")
    assert GITHUB_SOURCE_HINT not in body


def test_source_footer_line_covers_both_footer_forms() -> None:
    assert is_source_footer_line("原文标题：示例")
    assert is_source_footer_line(f"　　{GITHUB_SOURCE_HINT}")
    assert not is_source_footer_line("　　这是正文段落")


def test_rule_review_ignores_the_trailing_hint() -> None:
    draft = SimpleNamespace(
        body=f"{_github_body()}\n\n{GITHUB_SOURCE_HINT}",
        source_url="https://github.com/owner/repo",
        source_name="GitHub Trending",
        title_options_json=["owner/repo｜示例"],
        tags_json=["开源项目"],
        content_plan_json={},
        source_item=SimpleNamespace(source_kind="github"),
    )

    report = rule_review(draft)

    assert report["passed"] is True
    assert report["paragraph_count"] == 8


def test_wechat_render_never_appends_an_image_after_the_last_paragraph() -> None:
    body = f"第一段\n\n第二段\n\n第三段\n\n原文标题：示例"
    # 位置 3 等于“最后一段之后”，属于文末；必须被前移到最后一段之前。
    rendered = render_wechat_html(body, [{"url": "https://mmbiz.qpic.cn/a.png", "after_paragraph": 3}])

    assert rendered.index("a.png") < rendered.index("第三段")
    assert rendered.rindex("a.png") < rendered.rindex("原文标题")


def test_wechat_render_places_position_zero_at_the_beginning() -> None:
    rendered = render_wechat_html("第一段\n\n第二段", [{"url": "https://mmbiz.qpic.cn/b.png", "after_paragraph": 0}])

    assert rendered.index("b.png") < rendered.index("第一段")


@pytest.mark.parametrize("paragraphs", [2, 3, 4, 6, 7])
def test_illustration_plan_never_lands_after_the_last_paragraph(paragraphs: int) -> None:
    plan = fallback_plan(_github_body(paragraphs))

    assert all(position <= paragraphs - 1 for position in plan.placements)


def test_illustration_plan_skips_single_paragraph_bodies() -> None:
    assert fallback_plan(_github_body(1)).placements == []


def test_llm_planner_clamps_out_of_range_positions(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **_kwargs):
            content = json.dumps({"placements": [1, 3, 7]})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(illustration_planner, "OpenAI", FakeClient)
    settings = SimpleNamespace(
        llm_enabled=True,
        llm_model=None,
        openai_api_key=None,
        openai_base_url=None,
        illustration_planner_llm_model="planner-model",
        illustration_planner_openai_api_key="planner-key",
        illustration_planner_openai_base_url=None,
        content_llm_max_retries=0,
        content_llm_timeout_seconds=30,
        langsmith_tracing=False,
        langsmith_api_key=None,
    )
    draft = SimpleNamespace(id="draft-1", body=_github_body(4), title_options_json=["标题"])

    plan = IllustrationPlanner(settings).decide(draft)

    assert plan.placements == [1, 3]


def _planner_settings() -> SimpleNamespace:
    return SimpleNamespace(
        llm_enabled=True,
        llm_model=None,
        openai_api_key=None,
        openai_base_url=None,
        illustration_planner_llm_model="planner-model",
        illustration_planner_openai_api_key="planner-key",
        illustration_planner_openai_base_url=None,
        content_llm_max_retries=0,
        content_llm_timeout_seconds=30,
        langsmith_tracing=False,
        langsmith_api_key=None,
    )


def _planner_client(payload: object, captured: list[str] | None = None):
    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            if captured is not None:
                captured.append(kwargs["messages"][0]["content"])
            content = json.dumps(payload, ensure_ascii=False)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    return FakeClient


def test_llm_planner_picks_a_style_and_a_subject_inside_the_pools(monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        illustration_planner,
        "OpenAI",
        _planner_client(
            {"style_index": 7, "placements": [{"after_paragraph": 2, "subject_index": 5}]},
            captured,
        ),
    )
    draft = SimpleNamespace(
        id="draft-1",
        body=_github_body(6),
        title_options_json=["标题"],
        source_item=SimpleNamespace(source_kind="github"),
    )
    brief = load_brief(SourceKind.GITHUB)
    assert brief is not None

    plan = IllustrationPlanner(_planner_settings()).decide(draft)

    assert plan.placements == [2]
    assert plan.subjects == (brief.objects[4],)
    # 风格按模型给的起点逐张错开：单张时就是该编号对应的风格。
    assert plan.styles == (brief.styles[6],)
    # 两套池子都以带编号的清单出现在提示词里，模型只能返回编号。
    assert "风格清单" in captured[0]
    assert "实物清单" in captured[0]
    assert f"7. {brief.styles[6]}" in captured[0]
    assert f"5. {brief.objects[4]}" in captured[0]
    assert "style_index" in captured[0]


def test_llm_planner_falls_back_to_rotation_when_indexes_are_outside_the_pools(monkeypatch) -> None:
    monkeypatch.setattr(
        illustration_planner,
        "OpenAI",
        _planner_client({"style_index": 999, "placements": [{"after_paragraph": 2, "subject_index": 99}]}),
    )
    draft = SimpleNamespace(
        id="draft-1",
        body=_github_body(6),
        title_options_json=["标题"],
        source_item=SimpleNamespace(source_kind="github"),
    )
    brief = load_brief(SourceKind.GITHUB)
    assert brief is not None

    plan = IllustrationPlanner(_planner_settings()).decide(draft)

    # 越界编号不被采纳，但风格与主体仍在池内（按草稿 ID 轮换），模型无法自创池外内容。
    assert plan.placements == [2]
    assert plan.subjects[0] in brief.objects
    assert plan.subjects[0] == pick_object(brief, "draft-1", "inline", 2)
    assert plan.styles[0] in brief.styles
    assert plan.styles[0] == pick_style(brief, "draft-1", "inline", 2)


def test_llm_planner_still_accepts_legacy_integer_placements(monkeypatch) -> None:
    monkeypatch.setattr(illustration_planner, "OpenAI", _planner_client({"placements": [1, 3]}))
    draft = SimpleNamespace(
        id="draft-1",
        body=_github_body(6),
        title_options_json=["标题"],
        source_item=SimpleNamespace(source_kind="rss"),
    )

    plan = IllustrationPlanner(_planner_settings()).decide(draft)

    assert plan.placements == [1, 3]
    assert len(plan.subjects) == 2
    assert all(subject for subject in plan.subjects)
    # 模型没给 style_index 时按草稿 ID 轮换，且两张图的风格互不相同。
    assert all(plan.styles)
    assert plan.styles[0] != plan.styles[1]


def test_llm_planner_rotates_styles_and_avoids_repeating_a_category(monkeypatch) -> None:
    """同一篇的两张图不能共用风格，也不能都落在同一类实物上。"""
    monkeypatch.setattr(
        illustration_planner,
        "OpenAI",
        _planner_client(
            {
                "style_index": 1,
                "placements": [
                    {"after_paragraph": 1, "subject_index": 1},
                    {"after_paragraph": 3, "subject_index": 3},
                ],
            }
        ),
    )
    draft = SimpleNamespace(
        id="draft-1",
        body=_github_body(6),
        title_options_json=["标题"],
        source_item=SimpleNamespace(source_kind="github"),
    )
    brief = load_brief(SourceKind.GITHUB)
    assert brief is not None

    plan = IllustrationPlanner(_planner_settings()).decide(draft)

    assert plan.placements == [1, 3]
    assert plan.styles == (brief.styles[0], brief.styles[1])
    # 池内 1 与 3 都是 [desk] 类别，第二张会被换成其他类别的实物。
    categories = [brief.categories[brief.objects.index(subject)] for subject in plan.subjects]
    assert len(set(categories)) == len(categories)


class _FlowRepository:
    """审核→改稿流程所需的替身仓储。"""

    def __init__(self, *, passed_first: bool = False) -> None:
        self.passed_first = passed_first
        self.revisions = 0
        self.review_runs = 0
        self.events: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        self.approved = False
        # 改稿时的联网补充会并入草稿证据（审核据此核对事实）。
        self.appended_evidence: list[dict] = []
        self.session = SimpleNamespace(commit=lambda: None, rollback=lambda: None)

    def append_draft_evidence(self, draft_id: str, entries: list[dict]) -> int:
        self.appended_evidence.extend(entries)
        return len(entries)

    def mark_auto_review_run_running(self, review_id: str):
        return SimpleNamespace(id=review_id)

    def create_auto_review_run(self, *_args, **_kwargs):
        self.review_runs += 1
        return SimpleNamespace(id=f"review-{self.review_runs}")

    def get_draft(self, draft_id: str):
        return SimpleNamespace(
            id=draft_id,
            body="　　测试正文",
            source_url="https://example.com/source",
            source_name="Example",
            title_options_json=["测试标题"],
            tags_json=["科技资讯"],
            content_plan_json={},
            evidence_json=[],
            summary_cn="测试摘要",
            version=1,
            source_item=SimpleNamespace(title="示例原文", source_kind="rss"),
        )

    def finish_auto_review_run(self, review_id, status, *_args, **_kwargs) -> None:
        self.statuses.append((review_id, status))

    def add_chat_agent_event(self, _run_id, title, *_args, **_kwargs) -> None:
        self.events.append(title)

    def apply_auto_revision(self, *_args, **_kwargs):
        self.revisions += 1
        return SimpleNamespace(version=self.revisions + 1)

    def review_draft(self, draft_id, _command):
        self.approved = True
        return SimpleNamespace(id=draft_id)

    def get_active_draft_source_snapshot(self, _draft_id):
        return None


class _FakeSnapshotStore:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def delete_after_approval(self, _draft_id) -> bool:
        return False


def _review(passed: bool, issues: list[str]):
    return SimpleNamespace(
        passed=passed,
        rule_report={"failures": []},
        model_report={"issues": issues} if issues else {"issues": []},
        error_message=None,
    )


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "llm_enabled": True,
        "auto_wechat_draft_enabled": False,
        # 改稿前联网补充：这些流程用例不检索，保持确定性。
        "revision_search_enabled": False,
        "exa_mcp_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_passing_review_still_runs_one_revision_with_its_issues(monkeypatch) -> None:
    reviews = [_review(True, ["提法可以更自然"]), _review(True, [])]
    calls = {"review": 0}

    class FakeReviewTool:
        def __init__(self, *_args) -> None:
            pass

        def invoke(self, *_args, **_kwargs):
            result = reviews[min(calls["review"], len(reviews) - 1)]
            calls["review"] += 1
            return result

    class FakeRevisionTool:
        def __init__(self, *_args) -> None:
            pass

        def invoke(self, *_args, **_kwargs):
            return SimpleNamespace(summary_cn="摘要", body="正文", tags=["科技资讯"], article_shape={"paragraph_count": 4})

    async def fake_selection(*_args, **_kwargs):
        return SimpleNamespace(cover_asset_id="cover", inline_asset_ids_json=[])

    monkeypatch.setattr(auto_delivery, "AutoReviewTool", FakeReviewTool)
    monkeypatch.setattr(auto_delivery, "AutoRevisionTool", FakeRevisionTool)
    monkeypatch.setattr(auto_delivery, "DraftSourceSnapshotStore", _FakeSnapshotStore)
    monkeypatch.setattr(auto_delivery, "ensure_agent_selected_wechat_assets", fake_selection)
    repository = _FlowRepository()

    result = await auto_delivery.auto_review_and_create_wechat_draft(
        _settings(), repository, "draft-1", "chat-run-1", deliver=True
    )

    assert calls["review"] == 2
    assert repository.revisions == 1
    assert repository.approved is True
    assert result["status"] == "approved_no_delivery"
    assert any(event.startswith("自动改稿中") for event in repository.events)


@pytest.mark.asyncio
async def test_revision_search_uses_review_queries_and_feeds_the_revision(monkeypatch) -> None:
    """联网补充搭在改稿上：审核给出的名称用于检索，检索结果进入改稿提示词。"""
    revision_calls: list[dict] = []
    search_calls: list[list[str]] = []

    class FakeReviewTool:
        def __init__(self, *_args) -> None:
            pass

        def invoke(self, *_args, **_kwargs):
            return SimpleNamespace(
                passed=True,
                rule_report={"failures": []},
                model_report={"issues": ["Antigravity 未说明是什么"], "search_queries": ["Antigravity"]},
                error_message=None,
            )

    class FakeRevisionTool:
        def __init__(self, *_args) -> None:
            pass

        def invoke(self, *_args, **kwargs):
            revision_calls.append(kwargs)
            return SimpleNamespace(summary_cn="摘要", body="正文", tags=["科技资讯"], article_shape={"paragraph_count": 4})

    class FakeSearchTool:
        def __init__(self, *_args) -> None:
            pass

        async def search(self, queries):
            search_calls.append(list(queries))
            return [{"id": "exa-search-1", "title": "Exa 联网检索：Antigravity", "content": "Antigravity 是某公司的 agentic 开发平台。"}]

    async def fake_selection(*_args, **_kwargs):
        return SimpleNamespace(cover_asset_id="cover", inline_asset_ids_json=[])

    monkeypatch.setattr(auto_delivery, "AutoReviewTool", FakeReviewTool)
    monkeypatch.setattr(auto_delivery, "AutoRevisionTool", FakeRevisionTool)
    monkeypatch.setattr(auto_delivery, "ExaMcpSearchTool", FakeSearchTool)
    monkeypatch.setattr(auto_delivery, "revision_issues", lambda *_args: ["Antigravity 未说明是什么"])
    monkeypatch.setattr(auto_delivery, "DraftSourceSnapshotStore", _FakeSnapshotStore)
    monkeypatch.setattr(auto_delivery, "ensure_agent_selected_wechat_assets", fake_selection)

    await auto_delivery.auto_review_and_create_wechat_draft(
        _settings(revision_search_enabled=True, exa_mcp_enabled=True),
        _FlowRepository(),
        "draft-1",
        "chat-run-1",
        deliver=True,
    )

    assert search_calls == [["Antigravity"]]
    assert revision_calls[0]["search_evidence"][0]["id"] == "exa-search-1"


@pytest.mark.asyncio
async def test_revision_search_falls_back_to_names_in_the_body(monkeypatch) -> None:
    """审核没给检索词时，用正文里的外部名称兜底，仍不增加模型调用。"""
    search_calls: list[list[str]] = []

    class FakeReviewTool:
        def __init__(self, *_args) -> None:
            pass

        def invoke(self, *_args, **_kwargs):
            return SimpleNamespace(
                passed=True,
                rule_report={"failures": []},
                model_report={"issues": ["提法可以更自然"]},
                error_message=None,
            )

    class FakeRevisionTool:
        def __init__(self, *_args) -> None:
            pass

        def invoke(self, *_args, **_kwargs):
            return SimpleNamespace(summary_cn="摘要", body="正文", tags=["科技资讯"], article_shape={"paragraph_count": 4})

    class FakeSearchTool:
        def __init__(self, *_args) -> None:
            pass

        async def search(self, queries):
            search_calls.append(list(queries))
            return []

    monkeypatch.setattr(auto_delivery, "AutoReviewTool", FakeReviewTool)
    monkeypatch.setattr(auto_delivery, "AutoRevisionTool", FakeRevisionTool)
    monkeypatch.setattr(auto_delivery, "ExaMcpSearchTool", FakeSearchTool)
    monkeypatch.setattr(auto_delivery, "DraftSourceSnapshotStore", _FakeSnapshotStore)

    async def fake_selection(*_args, **_kwargs):
        return SimpleNamespace(cover_asset_id="cover", inline_asset_ids_json=[])

    monkeypatch.setattr(auto_delivery, "ensure_agent_selected_wechat_assets", fake_selection)

    repository = _FlowRepository()
    original_get_draft = repository.get_draft

    def draft_with_external_names(draft_id):
        # 只替换正文，保留 _model_draft_snapshot 需要的其余字段。
        snapshot = original_get_draft(draft_id)
        snapshot.body = "　　affaan-m/ECC 支持 Claude Code 与 OpenCode。"
        return snapshot

    monkeypatch.setattr(repository, "get_draft", draft_with_external_names)

    await auto_delivery.auto_review_and_create_wechat_draft(
        _settings(revision_search_enabled=True, exa_mcp_enabled=True),
        repository,
        "draft-1",
        "chat-run-1",
        deliver=True,
    )

    assert search_calls and "Claude Code" in search_calls[0]


@pytest.mark.asyncio
async def test_review_only_mode_skips_asset_selection_and_delivery(monkeypatch) -> None:
    selection_calls = {"count": 0}

    async def fake_selection(*_args, **_kwargs):
        selection_calls["count"] += 1
        return SimpleNamespace(cover_asset_id="cover", inline_asset_ids_json=[])

    async def forbidden_delivery(*_args, **_kwargs):
        raise AssertionError("仅审核模式不得创建公众号草稿")

    class FakeReviewTool:
        def __init__(self, *_args) -> None:
            pass

        def invoke(self, *_args, **_kwargs):
            return _review(True, [])

    monkeypatch.setattr(auto_delivery, "AutoReviewTool", FakeReviewTool)
    monkeypatch.setattr(auto_delivery, "DraftSourceSnapshotStore", _FakeSnapshotStore)
    monkeypatch.setattr(auto_delivery, "ensure_agent_selected_wechat_assets", fake_selection)
    monkeypatch.setattr(auto_delivery, "retry_agent_selected_wechat_draft", forbidden_delivery)
    repository = _FlowRepository()

    result = await auto_delivery.auto_review_and_create_wechat_draft(
        _settings(auto_wechat_draft_enabled=True), repository, "draft-1", "chat-run-1", deliver=False
    )

    assert selection_calls["count"] == 0
    assert repository.approved is True
    assert result["status"] == "approved_no_delivery"
    assert result["delivered"] is False
