"""指名抓取：模型给出 target → 只抓该仓库，不读 Trending 榜单。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.domain.models import AgentCollectCommand, ConversationDecision, SourceKind, normalize_github_target
from app.services.collection import CollectionBatch
from app.tools.contracts import CollectionToolRequest


def test_normalize_github_target_accepts_slugs_and_urls() -> None:
    assert normalize_github_target("affaan-m/ECC") == "affaan-m/ECC"
    assert normalize_github_target(" https://github.com/psf/requests ") == "psf/requests"
    assert normalize_github_target("http://www.github.com/psf/requests.git") == "psf/requests"
    assert normalize_github_target("github.com/psf/requests/issues/12") == "psf/requests"
    assert normalize_github_target("看看 psf/requests 这个项目。") is None  # 句子交给模型解析
    assert normalize_github_target("") is None
    assert normalize_github_target(None) is None


def test_agent_collect_command_normalizes_target_and_rejects_junk() -> None:
    command = AgentCollectCommand(sources=[SourceKind.GITHUB], limit=5, target="https://github.com/psf/requests")

    assert command.target == "psf/requests"
    # 不带 target 的榜单采集行为不变。
    assert AgentCollectCommand(sources=[SourceKind.GITHUB], limit=5).target is None
    with pytest.raises(ValueError):
        AgentCollectCommand(sources=[SourceKind.GITHUB], limit=5, target="not a repo")


def test_conversation_decision_carries_target() -> None:
    decision = ConversationDecision(
        intent="collect_news",
        reply="好的",
        sources=[SourceKind.GITHUB],
        limit=5,
        target="affaan-m/ECC",
    )

    assert decision.target == "affaan-m/ECC"


class _StubCollector:
    def __init__(self) -> None:
        self.collected = 0
        self.project_calls: list[str] = []

    async def collect(self, limit: int):  # pragma: no cover - 断言不会被调用
        self.collected += 1
        raise AssertionError("点名项目时不应读取榜单")

    async def fetch_project(self, external_id: str):
        self.project_calls.append(external_id)
        return SimpleNamespace(
            external_id=external_id,
            metadata={"readme_fetch_status": "success", "project_target": True},
        )


@pytest.mark.asyncio
async def test_source_tool_fetches_named_project_instead_of_trending() -> None:
    from app.tools.source_tools import SourceCollectionTool

    collector = _StubCollector()
    tool = SourceCollectionTool(SourceKind.GITHUB, collector)

    batch = await tool.invoke(CollectionToolRequest(source=SourceKind.GITHUB, limit=25, target="psf/requests"))

    assert collector.project_calls == ["psf/requests"]
    assert collector.collected == 0
    assert batch.selection_metadata == {"mode": "project", "target": "psf/requests"}
    assert batch.selected_items and batch.selected_items[0].external_id == "psf/requests"


@pytest.mark.asyncio
async def test_source_tool_without_target_still_uses_list_collection() -> None:
    from app.tools.source_tools import SourceCollectionTool

    class _ListCollector:
        async def collect(self, limit: int):
            return [SimpleNamespace(external_id="owner/repo", metadata={})]

    tool = SourceCollectionTool(SourceKind.GITHUB, _ListCollector())

    batch = await tool.invoke(CollectionToolRequest(source=SourceKind.GITHUB, limit=25))

    assert batch.selection_metadata == {}
    assert [item.external_id for item in batch.items] == ["owner/repo"]


def test_targeted_batch_is_recognized_and_skips_ranking_filter() -> None:
    """点名项目批次不参与榜单过滤，也不重复抓 README。"""
    from app.agents.content_main_agent import ContentMainAgent

    item = SimpleNamespace(
        external_id="psf/requests",
        metadata={"readme_fetch_status": "success", "project_target": True},
    )
    targeted = CollectionBatch(
        source_kind=SourceKind.GITHUB.value,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        items=[item],
        selected_items=[item],
        selection_metadata={"mode": "project", "target": "psf/requests"},
    )
    listed = CollectionBatch(
        source_kind=SourceKind.GITHUB.value,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        items=[item],
    )

    assert ContentMainAgent.is_targeted_batch(targeted) is True
    assert ContentMainAgent.is_targeted_batch(listed) is False


def test_collection_completion_reports_targeted_project() -> None:
    from app.worker import collection_completion

    result = SimpleNamespace(
        runs=[{"selection": {"mode": "project", "target": "psf/requests"}, "created_draft_ids": ["d1"]}],
        created=1,
        status=SimpleNamespace(value="completed"),
        source_errors=[],
    )

    _, summary, _ = collection_completion(result)

    assert "psf/requests" in summary
    assert "采集完成" not in summary
