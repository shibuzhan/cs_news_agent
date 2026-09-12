from types import SimpleNamespace

from app.domain.models import RawSourceItem, SourceKind
from app.services.generator import DeterministicDraftGenerator
from app.workflows import content_workflow
from app.workflows.content_workflow import ContentPipeline, build_content_graph


def test_graph_generates_structured_pending_review_content():
    raw = RawSourceItem(
        source_kind=SourceKind.HACKER_NEWS,
        external_id="42",
        title="New model release improves agent reliability",
        url="https://example.com/model-release",
        source_name="Hacker News",
        metrics={"score": 200, "comments": 50},
    )
    state = build_content_graph(DeterministicDraftGenerator()).invoke(
        {"raw_item": raw.model_dump(mode="json")}
    )
    assert state["eligible"] is True
    assert state["normalized_item"]["category"] == "产品更新"
    assert state["draft"]["source_url"] == "https://example.com/model-release"


def test_graph_skips_empty_signal():
    raw = RawSourceItem(
        source_kind=SourceKind.RSS,
        external_id="short",
        title="Hi",
        url="https://example.com/short",
        source_name="Example RSS",
    )
    state = build_content_graph(DeterministicDraftGenerator()).invoke(
        {"raw_item": raw.model_dump(mode="json")}
    )
    assert state["eligible"] is False
    assert "draft" not in state


def test_pipeline_regenerates_when_only_discarded_history_exists(monkeypatch):
    class FakeSession:
        def commit(self):
            return None

        def rollback(self):
            return None

    class FakeRepository:
        instance = None

        def __init__(self, _session) -> None:
            self.created = []
            FakeRepository.instance = self

        def save_source(self, _item):
            # 来源本身已经存在不应直接等同于草稿去重。
            return SimpleNamespace(is_duplicate=True, row=SimpleNamespace(id="source-1"))

        def get_deduplicating_draft_for_source(self, _source_id):
            # discarded/deleted 历史由仓储查询过滤，不阻止重新生成。
            return None

        def create_draft(self, _source, draft, _evidence):
            self.created.append(draft)
            return SimpleNamespace(id="draft-regenerated")

    monkeypatch.setattr(content_workflow, "ContentRepository", FakeRepository)
    raw = RawSourceItem(
        source_kind=SourceKind.RSS,
        external_id="discarded-source",
        title="Previously discarded topic can be regenerated",
        url="https://example.com/discarded-source",
        source_name="Example RSS",
        summary="A sufficiently detailed source item that should generate a new draft.",
        content="A sufficiently detailed source item that should generate a new draft.",
    )

    result = ContentPipeline(FakeSession(), DeterministicDraftGenerator()).process([raw])

    assert result["created"] == 1
    assert result["duplicates"] == 0
    assert len(FakeRepository.instance.created) == 1


def test_pipeline_skips_when_an_active_or_published_draft_exists(monkeypatch):
    class FakeSession:
        def commit(self):
            return None

        def rollback(self):
            return None

    class FakeRepository:
        instance = None

        def __init__(self, _session) -> None:
            self.created = []
            FakeRepository.instance = self

        def save_source(self, _item):
            return SimpleNamespace(is_duplicate=False, row=SimpleNamespace(id="source-1"))

        def get_deduplicating_draft_for_source(self, _source_id):
            return SimpleNamespace(id="published-draft", status="published")

        def create_draft(self, _source, draft, _evidence):
            self.created.append(draft)
            return SimpleNamespace(id="should-not-create")

    monkeypatch.setattr(content_workflow, "ContentRepository", FakeRepository)
    raw = RawSourceItem(
        source_kind=SourceKind.RSS,
        external_id="published-source",
        title="Previously published topic remains deduplicated",
        url="https://example.com/published-source",
        source_name="Example RSS",
        summary="A sufficiently detailed source item that should remain deduplicated.",
        content="A sufficiently detailed source item that should remain deduplicated.",
    )

    result = ContentPipeline(FakeSession(), DeterministicDraftGenerator()).process([raw])

    assert result["created"] == 0
    assert result["duplicates"] == 1
    assert FakeRepository.instance.created == []


def test_github_aggregate_keeps_constructor_generator(monkeypatch):
    class FakeSession:
        def __init__(self) -> None:
            self.commits = 0
            self.rollbacks = 0

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

    class FakeRepository:
        instance = None

        def __init__(self, _session) -> None:
            self.saved = []
            self.drafts = []
            FakeRepository.instance = self

        def save_source(self, item):
            self.saved.append(item)
            return SimpleNamespace(is_duplicate=False, row=SimpleNamespace(id=f"source-{len(self.saved)}"))

        def get_deduplicating_draft_for_source(self, _source_id):
            return None

        def create_draft(self, source, draft, evidence):
            self.drafts.append((source, draft, evidence))

    monkeypatch.setattr(content_workflow, "ContentRepository", FakeRepository)
    raw_items = [
        RawSourceItem(
            source_kind=SourceKind.GITHUB,
            external_id=f"example/project-{index}",
            title=f"Project {index} for agent workflows",
            url=f"https://github.com/example/project-{index}",
            summary="An open source project with enough description for a useful technology news brief.",
            content="An open source project with enough description for a useful technology news brief.",
            source_name="GitHub Trending",
            metrics={"stars_total": 1000 + index, "stars_period": 100 + index},
        )
        for index in range(3)
    ]

    session = FakeSession()
    result = ContentPipeline(session, DeterministicDraftGenerator()).process_github_aggregate(
        raw_items, top_n=2
    )

    assert result["created"] == 1
    assert result["errors"] == []
    assert result["aggregated"] == 2
    assert len(FakeRepository.instance.drafts) == 1
    assert len(FakeRepository.instance.drafts[0][2]) == 2
    assert session.commits == 1


def test_github_single_draft_is_not_marked_introduced_before_publication(monkeypatch):
    class NestedTransaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeSession:
        def begin_nested(self):
            return NestedTransaction()

    class FakeRepository:
        instance = None

        def __init__(self, _session) -> None:
            self.introduction_calls = 0
            FakeRepository.instance = self

        def save_source(self, _item):
            return SimpleNamespace(row=SimpleNamespace(id="source-1"))

        def get_deduplicating_draft_for_source(self, _source_id):
            return None

        def create_draft(self, _source, _draft, _evidence):
            return SimpleNamespace(id="draft-1")

        def record_project_introduction(self, *_args):
            self.introduction_calls += 1
            raise AssertionError("草稿生成不应写入已介绍记录")

    monkeypatch.setattr(content_workflow, "ContentRepository", FakeRepository)
    raw = RawSourceItem(
        source_kind=SourceKind.GITHUB,
        external_id="example/published-later",
        title="Published later",
        url="https://github.com/example/published-later",
        summary="A detailed project description suitable for a standalone draft.",
        content="A detailed project README suitable for a standalone draft with source-backed facts." * 8,
        source_name="GitHub Trending",
        # 生成前必须确认 README 已取到，否则流水线会拒绝生成。
        metadata={"readme_fetch_status": "success"},
    )

    result = ContentPipeline(FakeSession(), DeterministicDraftGenerator()).process_github_single(
        [raw], candidate_count=1
    )

    assert result["created"] == 1
    assert FakeRepository.instance.introduction_calls == 0


def test_github_item_without_readme_is_not_generated(monkeypatch):
    """只有 Trending 简介时必须直接失败并给出原因，而不是靠空话凑满字数。"""
    from app.services.model_errors import README_UNAVAILABLE_REASON

    class NestedTransaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeSession:
        def begin_nested(self):
            return NestedTransaction()

    class FakeRepository:
        def __init__(self, _session) -> None:
            pass

        def save_source(self, _item):
            raise AssertionError("缺少 README 时不应保存来源或生成草稿")

    monkeypatch.setattr(content_workflow, "ContentRepository", FakeRepository)
    raw = RawSourceItem(
        source_kind=SourceKind.GITHUB,
        external_id="owner/thin",
        title="owner/thin",
        url="https://github.com/owner/thin",
        summary="The agent harness performance optimization system.",
        content="The agent harness performance optimization system.",
        source_name="GitHub Trending",
        metadata={"readme_fetch_status": "failed", "readme_fetch_error": "rate_limited"},
    )

    result = ContentPipeline(FakeSession(), DeterministicDraftGenerator()).process_github_single(
        [raw], candidate_count=1
    )

    assert result["created"] == 0
    assert result["errors"] == [{"external_id": "owner/thin", "error": README_UNAVAILABLE_REASON}]
    assert "README" in result["errors"][0]["error"]


class _ProviderRejection(Exception):
    """替身：思考模式模型拒绝 tool_choice 的 400 响应。"""

    def __init__(self) -> None:
        super().__init__(
            "Error code: 400 - {'error': {'message': 'The tool_choice parameter does not support being "
            "set to required or object in thinking mode'}, 'id': 'chatcmpl-b140990b'}"
        )
        self.status_code = 400
        self.body = {
            "error": {
                "code": "invalid_parameter_error",
                "message": "The tool_choice parameter does not support being set to required or object "
                "in thinking mode",
            }
        }


def test_pipeline_stores_a_sanitized_failure_reason(monkeypatch):
    """落库与审计里的失败原因只能是分类短句，不能是供应商原始响应。"""

    class FakeSession:
        def commit(self):
            return None

        def rollback(self):
            return None

    class FakeRepository:
        instance = None

        def __init__(self, _session) -> None:
            FakeRepository.instance = self

        def save_source(self, _item):
            return SimpleNamespace(is_duplicate=False, row=SimpleNamespace(id="source-1"))

        def get_deduplicating_draft_for_source(self, _source_id):
            return None

        def create_draft(self, *_args):
            raise AssertionError("生成失败时不应创建草稿")

    class RejectingGenerator:
        def generate(self, _item):
            raise _ProviderRejection()

    monkeypatch.setattr(content_workflow, "ContentRepository", FakeRepository)
    raw = RawSourceItem(
        source_kind=SourceKind.RSS,
        external_id="provider-rejection",
        title="Provider rejects the structured output request",
        url="https://example.com/provider-rejection",
        source_name="Example RSS",
        summary="A source item that triggers a provider rejection during generation.",
        content="A source item that triggers a provider rejection during generation.",
    )

    result = ContentPipeline(FakeSession(), RejectingGenerator()).process([raw])

    assert result["created"] == 0
    assert len(result["errors"]) == 1
    stored = result["errors"][0]["error"]
    assert "思考模式" in stored
    assert "chatcmpl" not in stored
    assert "Error code" not in stored
