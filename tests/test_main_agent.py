from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents import content_main_agent
from app.agents.content_main_agent import AgentCommandError, ContentMainAgent
from app.domain.models import AgentCollectCommand, AgentRunStatus, RawSourceItem, SourceKind
from app.services.collection import CollectionBatch
from app.services.generator import DeterministicDraftGenerator
from app.tools.contracts import CollectionToolResult


class FakeTool:
    def __init__(self, source: SourceKind, error: str | None = None):
        self.source = source
        self.error = error
        self.calls = 0

    async def invoke(self, request):
        self.calls += 1
        now = datetime.now(UTC)
        return CollectionBatch(
            source_kind=self.source.value,
            started_at=now,
            finished_at=now,
            items=[],
            error=self.error,
        )

    def result_from_batch(self, batch):
        return CollectionToolResult(
            source=self.source,
            status="failed" if batch.error else "collected",
            item_count=len(batch.items),
            started_at=batch.started_at,
            finished_at=batch.finished_at,
            error=batch.error,
        )

    async def enrich_items(self, items):
        return items


@pytest.mark.asyncio
async def test_main_agent_runs_requested_tools_and_keeps_failure_isolated():
    arxiv = FakeTool(SourceKind.ARXIV)
    github = FakeTool(SourceKind.GITHUB, error="network unavailable")
    finished = []

    def persist(batch, _generator, agent_run_id):
        if batch.error:
            return {
                "run_id": f"{agent_run_id}-{batch.source_kind}",
                "source": batch.source_kind,
                "status": "failed",
                "error": batch.error,
            }
        return {
            "run_id": f"{agent_run_id}-{batch.source_kind}",
            "source": batch.source_kind,
            "status": "success",
            "received": 0,
            "created": 0,
            "duplicates": 0,
            "skipped": 0,
        }

    agent = ContentMainAgent(
        {SourceKind.ARXIV: arxiv, SourceKind.GITHUB: github},
        DeterministicDraftGenerator(),
        persist_batch=persist,
        create_run=lambda _command: "agent-run-1",
        finish_run=lambda *args: finished.append(args),
    )

    result = await agent.run(
        AgentCollectCommand(sources=[SourceKind.ARXIV, SourceKind.GITHUB], limit=2)
    )

    assert arxiv.calls == 1
    assert github.calls == 1
    assert result.status == AgentRunStatus.PARTIAL
    assert result.source_errors == [{"source": "github", "error": "network unavailable"}]
    assert finished[0][1] == AgentRunStatus.PARTIAL


@pytest.mark.asyncio
async def test_main_agent_rejects_unregistered_source():
    finished = []
    agent = ContentMainAgent(
        {SourceKind.ARXIV: FakeTool(SourceKind.ARXIV)},
        DeterministicDraftGenerator(),
        create_run=lambda _command: "agent-run-2",
        finish_run=lambda *args: finished.append(args),
    )

    with pytest.raises(AgentCommandError, match="来源不可用：hacker_news"):
        await agent.run(AgentCollectCommand(sources=[SourceKind.HACKER_NEWS], limit=2))

    assert finished[0][1] == AgentRunStatus.FAILED


def test_main_agent_command_rejects_duplicate_sources():
    with pytest.raises(ValueError, match="sources 不能包含重复来源"):
        AgentCollectCommand(sources=[SourceKind.ARXIV, SourceKind.ARXIV], limit=2)


@pytest.mark.asyncio
async def test_main_agent_selects_one_new_github_project_before_readme_tool():
    class GithubTool(FakeTool):
        def __init__(self):
            super().__init__(SourceKind.GITHUB)
            self.enriched = []

        async def invoke(self, _request):
            now = datetime.now(UTC)
            items = [
                RawSourceItem(
                    source_kind=SourceKind.GITHUB,
                    external_id=f"example/project-{index}",
                    title=f"Project {index}",
                    url=f"https://github.com/example/project-{index}",
                    summary="A project description with sufficient detail.",
                    content="A project description with sufficient detail.",
                    source_name="GitHub Trending",
                    metrics={"stars_period": index, "stars_total": 100 + index},
                )
                for index in range(1, 4)
            ]
            return CollectionBatch("github", now, now, items)

        async def enrich_items(self, items):
            self.enriched = items
            return items

    tool = GithubTool()
    persisted = []
    agent = ContentMainAgent(
        {SourceKind.GITHUB: tool},
        DeterministicDraftGenerator(),
        persist_batch=lambda batch, _generator, _run_id: persisted.append(batch) or {
            "run_id": "github-run", "source": "github", "status": "success",
            "received": len(batch.items), "created": 1, "duplicates": 0, "skipped": 2,
        },
        create_run=lambda _command: "agent-run-3",
        finish_run=lambda *_args: None,
        github_candidate_filter=lambda items: ([items[-1]], 2),
    )

    result = await agent.run(AgentCollectCommand(sources=[SourceKind.GITHUB], limit=3))

    assert [item.external_id for item in tool.enriched] == ["example/project-3"]
    assert persisted[0].selection_metadata["excluded_before_rank"] == 2
    assert result.created == 1


def test_github_candidates_exclude_active_drafts_before_sorting(monkeypatch):
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeRepository:
        def __init__(self, _session):
            pass

        def deduplicating_github_candidate_external_ids(self, _external_ids):
            return {"example/most-starred"}

    monkeypatch.setattr(content_main_agent, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(content_main_agent, "ContentRepository", FakeRepository)
    items = [
        RawSourceItem(
            source_kind=SourceKind.GITHUB,
            external_id="example/most-starred",
            title="Most starred but already drafted",
            url="https://github.com/example/most-starred",
            summary="A sufficiently detailed project description.",
            content="A sufficiently detailed project description.",
            source_name="GitHub Trending",
            metrics={"stars_period": 500, "stars_total": 10_000},
            metadata={"periods": {"daily": 1}},
        ),
        RawSourceItem(
            source_kind=SourceKind.GITHUB,
            external_id="example/new-project",
            title="New project",
            url="https://github.com/example/new-project",
            summary="A sufficiently detailed project description.",
            content="A sufficiently detailed project description.",
            source_name="GitHub Trending",
            metrics={"stars_period": 100, "stars_total": 1_000},
            metadata={"periods": {"daily": 2}},
        ),
    ]

    selected, excluded_count = content_main_agent.select_unintroduced_github_projects(items)

    assert excluded_count == 1
    assert [item.external_id for item in selected] == ["example/new-project"]
