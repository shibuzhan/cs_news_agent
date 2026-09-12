from datetime import UTC, datetime

from app.domain.models import ContentCategory, RawSourceItem, SourceKind
from app.services.classifier import classify_item
from app.services.generator import DeterministicDraftGenerator
from app.services.normalizer import has_meaningful_content, normalize_item
from app.services.ranker import calculate_hot_score


def sample_item(kind: SourceKind = SourceKind.GITHUB) -> RawSourceItem:
    return RawSourceItem(
        source_kind=kind,
        external_id="example/hot-agent",
        title="  <b>Hot Agent</b>  ",
        url="https://github.com/example/hot-agent",
        author="example",
        published_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        summary="<p>A production-oriented open source agent framework.</p>",
        content="A production-oriented open source agent framework.",
        source_name="GitHub Trending",
        metrics={"stars_period": 321, "stars_total": 12345},
    )


def test_normalize_classify_rank_and_generate_preserve_source():
    item = normalize_item(sample_item())
    assert item.title == "Hot Agent"
    assert has_meaningful_content(item)

    classification = classify_item(item)
    item.category = classification.category
    item.category_confidence = classification.confidence
    item.hot_score = calculate_hot_score(item, datetime(2026, 9, 2, tzinfo=UTC))
    draft = DeterministicDraftGenerator().generate(item)

    assert item.category == ContentCategory.OPEN_SOURCE
    assert item.hot_score > 300
    assert str(item.url) not in draft.body
    # GitHub 文案不再追加“原文标题”尾注：项目名已出现在标题与正文开头。
    assert "原文标题：" not in draft.body
    assert str(draft.source_url) == str(item.url)


def test_non_github_body_keeps_a_single_source_title_footer():
    item = normalize_item(sample_item(SourceKind.RSS))
    item.category = ContentCategory.INDUSTRY_NEWS
    draft = DeterministicDraftGenerator().generate(item)

    assert draft.body.count("原文标题：") == 1
    assert str(item.url) not in draft.body


def test_content_hash_is_stable_for_equivalent_html():
    first = normalize_item(sample_item())
    raw = sample_item()
    raw.title = "Hot Agent"
    raw.summary = "A production-oriented open source agent framework."
    second = normalize_item(raw)
    assert first.content_hash == second.content_hash
