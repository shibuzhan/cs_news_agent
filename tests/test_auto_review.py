from types import SimpleNamespace

import pytest

import app.services.auto_delivery as auto_delivery
from app.services.wechat_official import render_wechat_html
from app.tools.auto_review import _blocking_issue_count, _review_score, rule_review
from app.tools.auto_revision import revision_issues
from app.services.model_errors import model_failure_message
from app.services.plain_text import append_source_title, compose_natural_article
from app.tools.illustration_planner import AutoIllustrationTool, fallback_plan
from app.tools.image_generation import GeneratedIllustration


def test_rule_review_accepts_required_natural_article_shape() -> None:
    paragraphs = [
        "　　这是第一段正文，用足够自然的中文介绍背景与关键概念。" * 12,
        "　　这是第二段正文，解释实现方式和普通读者需要知道的含义。" * 12,
        "　　这是第三段正文，补充实际使用场景、限制和需要关注的风险。" * 12,
        "　　这是第四段正文，用自然语言说明后续观察方向而不夸大结论。" * 12,
    ]
    draft = SimpleNamespace(
        body="\n\n".join(paragraphs + ["原文标题：示例原文"]),
        source_url="https://example.com/source",
        source_name="Example",
        title_options_json=["示例标题"],
        tags_json=["科技资讯"],
    )
    report = rule_review(draft)
    assert report["passed"] is True
    assert report["paragraph_count"] == 4


def test_natural_article_can_use_more_than_four_paragraphs_without_logical_section_array() -> None:
    body, shape = compose_natural_article("\n\n".join([
        "这是背景段，交代读者会遇到的实际问题。" * 12,
        "这是技术过程的第一段，解释关键做法。" * 12,
        "这是技术过程的第二段，补充实现细节与约束。" * 12,
        "这是价值与边界段，说明适用场景和仍需注意的限制。" * 12,
        "这是后续观察段，说明下一步值得跟进的信息。" * 12,
    ]))
    draft = SimpleNamespace(
        body=append_source_title(body, "示例原文"),
        source_url="https://example.com/source",
        source_name="Example",
        title_options_json=["示例标题"],
        tags_json=["科技资讯"],
        content_plan_json={"article_shape": shape},
    )

    report = rule_review(draft)

    assert report["passed"] is True
    assert report["paragraph_count"] == 5
    assert report["article_shape"]["paragraph_count"] == 5


def test_wechat_html_inserts_positioned_image_after_requested_paragraph() -> None:
    rendered = render_wechat_html(
        "第一段\n\n第二段",
        [{"url": "https://images.example.com/chart.png", "after_paragraph": 1}],
    )
    assert "<p>　　第一段</p>" in rendered
    assert "<p>　　第二段</p>" in rendered
    assert rendered.index("第一段") < rendered.index("chart.png") < rendered.index("第二段")


def test_deterministic_illustration_plan_decides_count_and_positions_from_article_shape() -> None:
    body = "\n\n".join([f"　　第 {index} 段正文" for index in range(1, 6)])
    plan = fallback_plan(body)
    assert plan.mode == "deterministic"
    assert plan.placements == [2, 4]


@pytest.mark.asyncio
async def test_auto_illustration_plans_before_generating_and_binds_each_position(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, int, str, str]] = []

    class Repository:
        def get_draft(self, _draft_id):
            return SimpleNamespace(
                id="draft-1",
                body="\n\n".join([f"　　第 {index} 段正文" for index in range(1, 6)]),
            )

    class ImageTool:
        def __init__(self, *_args) -> None:
            pass

        async def invoke(self, draft_id, purpose, position, context, visual_direction=""):
            calls.append((draft_id, purpose, position, context, visual_direction))
            return GeneratedIllustration(f"illustration-{position}", f"asset-{position}", purpose, position)

    monkeypatch.setattr("app.tools.illustration_planner.ImageGenerationTool", ImageTool)
    result = await AutoIllustrationTool(
        SimpleNamespace(llm_enabled=False, openai_api_key=None, llm_model=None), Repository()
    ).invoke("draft-1")

    assert result["placements"] == [2, 4]
    assert [call[2] for call in calls] == [2, 4]
    assert calls[0][4] != calls[1][4]
    assert result["generated_illustration_ids"] == ["illustration-2", "illustration-4"]


def test_revision_issues_keeps_rule_and_model_feedback() -> None:
    issues = revision_issues(
        {"failures": ["正文长度不足"]},
        {"issues": ["术语需要解释"], "summary": "不应作为改稿指令"},
    )
    assert issues == ["正文长度不足", "术语需要解释"]


def test_model_timeout_has_a_safe_specific_message() -> None:
    timeout = type("APITimeoutError", (Exception,), {})()
    assert model_failure_message(timeout, "自动改稿", 20) == "自动改稿模型请求超时（20 秒）"


def test_review_score_requires_range_and_blocking_issue_is_detected() -> None:
    assert _review_score(85) == 85
    assert _blocking_issue_count([
        {"type": "来源缺失", "severity": "critical", "description": "没有来源"},
        {"type": "措辞", "severity": "minor", "description": "可精简"},
    ]) == 1
    with pytest.raises(ValueError):
        _review_score(101)


@pytest.mark.asyncio
async def test_auto_review_rewrites_once_then_keeps_failed_final_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class Repository:
        def __init__(self) -> None:
            self.review_runs = 0
            self.revisions = 0
            self.events: list[str] = []

        def create_auto_review_run(self, *_args):
            self.review_runs += 1
            return SimpleNamespace(id=f"review-{self.review_runs}")

        def mark_auto_review_run_running(self, review_id):
            return SimpleNamespace(id=review_id)

        def get_draft(self, _draft_id):
            return SimpleNamespace(
                body="　　测试正文",
                source_url="https://example.com/source",
                source_name="Example",
                title_options_json=["测试标题"],
                tags_json=["科技资讯"],
                content_plan_json={},
                evidence_json=[],
                summary_cn="测试摘要",
                source_item=SimpleNamespace(title="示例原文", source_kind="rss"),
            )

        def finish_auto_review_run(self, *_args, **_kwargs) -> None:
            return None

        def add_chat_agent_event(self, _run_id, title, *_args, **_kwargs) -> None:
            self.events.append(title)

        def apply_auto_revision(self, *_args, **_kwargs):
            self.revisions += 1
            return SimpleNamespace(version=self.revisions + 1)

    class RejectingReview:
        def __init__(self, *_args) -> None:
            pass

        def invoke(self, _draft_id, **_kwargs):
            return SimpleNamespace(
                passed=False,
                rule_report={"failures": []},
                model_report={"issues": ["术语没有解释"]},
                error_message=None,
            )

    class RevisingTool:
        def __init__(self, *_args) -> None:
            pass

        def invoke(self, *_args, **_kwargs):
            return SimpleNamespace(
                summary_cn="摘要", body="正文", tags=["科技资讯"],
                article_shape={"paragraph_count": 4, "body_chars": 2},
            )

    monkeypatch.setattr(auto_delivery, "AutoReviewTool", RejectingReview)
    monkeypatch.setattr(auto_delivery, "AutoRevisionTool", RevisingTool)
    async def selected_assets(*_args):
        return SimpleNamespace(cover_asset_id="cover-1", inline_asset_ids_json=["inline-1"])

    monkeypatch.setattr(auto_delivery, "ensure_agent_selected_wechat_assets", selected_assets)
    repository = Repository()

    result = await auto_delivery.auto_review_and_create_wechat_draft(
        SimpleNamespace(), repository, "draft-1", "chat-run-1"
    )

    assert result["status"] == "failed"
    assert result["revision_count"] == 1
    assert repository.review_runs == 2
    assert repository.revisions == 1
    assert repository.events.count("自动改稿中（1/1）") == 1
