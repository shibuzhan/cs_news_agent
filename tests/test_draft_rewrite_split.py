"""② 拆分：`refresh_draft_source`（只换证据） + `regenerate_draft_body`（只用已存证据重写）。

真实动机：`rewrite_draft` 一次做两件事——“重新抓来源”和“重写正文”。用户想先确认来源
是不是最新的、再决定要不要重写时，只能整篇重写；而重写本身要跑一次 9–12 分钟的内容模型。
拆开之后 Agent 有了零件：刷新来源秒级返回且**不动正文**，重写则严格只用已保存证据。
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from app.agent_tools import draft_actions
from app.agent_tools.draft_actions import build_draft_action_tools
from app.domain.models import RawSourceItem, SourceKind
from app.services import source_refresh
from app.services.source_refresh import fetch_live_source


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeSource:
    id = "source-1"
    source_kind = "github"
    external_id = "openai/plugins"
    title = "openai/plugins"
    url = "https://github.com/openai/plugins"
    author = None
    published_at = None
    summary = "插件示例库"
    content = "# 旧 README\n\n" + "旧正文。" * 60
    source_name = "GitHub"
    metrics_json: dict = {}
    metadata_json = {"readme_fetch_status": "success", "content_origin": "github_readme"}


class FakeRepository:
    def __init__(self) -> None:
        self.refreshed: list[dict] = []
        self.snapshots: list[tuple[str, str]] = []
        self.saved: list[str] = []
        self.session = FakeSession()

    def get_draft(self, draft_id: str):
        return SimpleNamespace(
            id=draft_id,
            version=1,
            status="pending_review",
            title_options_json=["openai/plugins：示例"],
            source_item=FakeSource(),
        )

    def save_source(self, item) -> None:
        self.saved.append(item.content)

    def record_source_refresh(self, draft_id: str, **kwargs):  # noqa: ANN003
        self.refreshed.append({"draft_id": draft_id, **kwargs})
        return SimpleNamespace(id=draft_id, version=2, title_options_json=["openai/plugins：示例"]), False


class FakeSnapshotStore:
    def __init__(self, settings, repository) -> None:
        self.repository = repository

    def replace_github_readme(self, draft_id: str, item) -> bool:
        self.repository.snapshots.append((draft_id, item.content))
        return True


class FakeTool:
    def __init__(self, source_kind, refreshed=None, error: Exception | None = None) -> None:
        self.source_kind = source_kind
        self.refreshed = refreshed
        self.error = error
        self.calls = 0

    async def enrich_items(self, items):  # noqa: ANN001
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [self.refreshed]


def make_refreshed(content: str) -> RawSourceItem:
    return RawSourceItem(
        source_kind=SourceKind.GITHUB,
        external_id="openai/plugins",
        title="openai/plugins",
        url="https://github.com/openai/plugins",
        published_at=None,
        summary="插件示例库",
        content=content,
        source_name="GitHub",
        metadata={"readme_fetch_status": "success", "content_origin": "github_readme"},
    )


def test_fetch_live_source_rejects_sources_that_cannot_be_refetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """采集器没实现富化时会原样返回入参：不能当成“刷新成功”，否则用户以为来源已更新。"""
    item = make_refreshed("正文")
    monkeypatch.setattr(source_refresh, "build_source_tools", lambda *_a, **_k: {SourceKind.GITHUB: FakeTool(SourceKind.GITHUB, refreshed=item)})

    with pytest.raises(ValueError, match="不支持重新获取"):
        asyncio.run(fetch_live_source(SimpleNamespace(request_timeout_seconds=5), item))


def test_fetch_live_source_rejects_a_readmeless_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    item = make_refreshed("# 旧\n" + "正文。" * 60)
    tool = FakeTool(SourceKind.GITHUB, refreshed=make_refreshed("太短了"))
    monkeypatch.setattr(source_refresh, "build_source_tools", lambda *_a, **_k: {SourceKind.GITHUB: tool})

    with pytest.raises(ValueError, match="README"):
        asyncio.run(fetch_live_source(SimpleNamespace(request_timeout_seconds=5), item))


def run_refresh(monkeypatch: pytest.MonkeyPatch, repository: FakeRepository, tool: FakeTool):
    import app.services.source_snapshots as snapshot_module
    import app.services.normalizer as normalizer_module

    session = FakeSession()
    monkeypatch.setattr(draft_actions, "SessionLocal", lambda: session)
    monkeypatch.setattr(draft_actions, "ContentRepository", lambda _session: repository)
    monkeypatch.setattr(snapshot_module, "DraftSourceSnapshotStore", FakeSnapshotStore)
    monkeypatch.setattr(normalizer_module, "normalize_item", lambda item: SimpleNamespace(content=item.content))
    monkeypatch.setattr(draft_actions, "_resolve_draft", lambda *_a, **_k: (repository.get_draft("draft-1"), ""))
    monkeypatch.setattr(draft_actions, "_task_run", lambda *_a, **_k: SimpleNamespace(id="run-intake"))
    monkeypatch.setattr(source_refresh, "build_source_tools", lambda *_a, **_k: {SourceKind.GITHUB: tool})
    monkeypatch.setattr(draft_actions, "get_settings", lambda: SimpleNamespace(request_timeout_seconds=5))

    tools = {item.name: item for item in build_draft_action_tools("session-1", chat_run_id="run-intake")}
    result = tools["refresh_draft_source"].invoke({})
    return result, session


def test_refresh_draft_source_replaces_evidence_without_touching_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository()
    fresh = "# 新 README\n\n" + "新正文。" * 80
    tool = FakeTool(SourceKind.GITHUB, refreshed=make_refreshed(fresh))

    result, session = run_refresh(monkeypatch, repository, tool)

    assert result["status"] == "refreshed"
    assert result["body_changed"] is False
    assert "regenerate_draft_body" in result["message"], "要明确告诉用户下一步该调哪个工具"
    assert tool.calls == 1
    assert repository.saved and "新正文" in repository.saved[0], "来源记录要写回新正文（重写与展示都读它）"
    assert repository.snapshots and "新正文" in repository.snapshots[0][1], "私有快照要换成新正文"
    assert repository.refreshed and repository.refreshed[0]["draft_id"] == "draft-1"
    assert repository.refreshed[0]["chars"] >= 200
    assert result["draft_version"] == 2, "来源变了就登记一版，运营才能对上“当时用的是哪份来源”"
    assert session.commits == 1


def test_refresh_draft_source_keeps_old_evidence_when_fetching_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository()
    tool = FakeTool(SourceKind.GITHUB, error=RuntimeError("github 503"))

    result, session = run_refresh(monkeypatch, repository, tool)

    assert result["status"] == "failed"
    assert "503" in result["message"] and "保持原样" in result["message"]
    assert repository.saved == [] and repository.snapshots == [] and repository.refreshed == []
    assert session.commits == 0, "抓取失败不应该提交任何改动"


def test_regenerate_draft_body_requests_snapshot_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`regenerate_draft_body` 必须明确走 snapshot 模式，否则 worker 会联网重抓出另一份正文。"""
    monkeypatch.setattr(draft_actions, "_resolve_draft", lambda *_a, **_k: (FakeRepository().get_draft("draft-1"), ""))
    monkeypatch.setattr(draft_actions, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        draft_actions, "ContentRepository", lambda _session: FakeRepository()
    )

    captured: dict = {}

    async def fake_start_rewrite(_settings, _repository, session_id, draft, **kwargs):  # noqa: ANN001
        captured.update({"session_id": session_id, "draft_id": draft.id, **kwargs})
        return {"status": "started"}

    monkeypatch.setattr(draft_actions, "_start_rewrite", fake_start_rewrite)
    monkeypatch.setattr(draft_actions, "get_settings", lambda: SimpleNamespace())

    tools = {item.name: item for item in build_draft_action_tools("session-1", chat_run_id="run-intake")}
    result = tools["regenerate_draft_body"].invoke({})

    assert result["status"] == "started"
    assert captured["source_mode"] == "snapshot"
    assert captured["chat_run_id"] == "run-intake"


def test_rewrite_draft_keeps_the_auto_source_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """合并入口 `rewrite_draft` 保持原行为：缺快照时允许联网重抓。"""
    monkeypatch.setattr(draft_actions, "_resolve_draft", lambda *_a, **_k: (FakeRepository().get_draft("draft-1"), ""))
    monkeypatch.setattr(draft_actions, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        draft_actions, "ContentRepository", lambda _session: FakeRepository()
    )

    captured: dict = {}

    async def fake_start_rewrite(_settings, _repository, _session_id, _draft, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return {"status": "started"}

    monkeypatch.setattr(draft_actions, "_start_rewrite", fake_start_rewrite)
    monkeypatch.setattr(draft_actions, "get_settings", lambda: SimpleNamespace())

    tools = {item.name: item for item in build_draft_action_tools("session-1")}
    tools["rewrite_draft"].invoke({})

    assert captured["source_mode"] == "auto"


def test_regeneration_job_only_skips_the_network_in_snapshot_mode() -> None:
    """worker 里的分支必须显式区分：snapshot 模式缺证据就停，auto 模式才联网。"""
    from app import worker

    source = inspect.getsource(worker.process_draft_regeneration_job)

    assert 'if mode == "auto"' in source, "只有 auto 模式才做“旧 README 转存快照”的兼容动作"
    assert "fetch_live_source" in source
    assert 'elif mode == "snapshot"' in source and "raise ValueError" in source
    assert 'source_mode: str = "auto"' in source
    assert '"source_mode": mode' in source, "事件里要留下用了哪份来源，便于排查"


def test_snapshot_mode_refuses_when_there_is_no_saved_evidence() -> None:
    from app import worker

    repository = SimpleNamespace(get_active_draft_source_snapshot=lambda _draft_id: None)
    raw = SimpleNamespace(content="只有 Trending 简介", source_kind=SourceKind.GITHUB)

    assert worker._source_from_snapshot(repository, "draft-1", raw) is None

    raw_with_body = SimpleNamespace(content="正文。" * 200, source_kind=SourceKind.GITHUB)
    assert worker._source_from_snapshot(repository, "draft-1", raw_with_body) is raw_with_body


def test_job_enqueue_forwards_the_source_mode() -> None:
    """参数必须一路传到底：中间任何一层丢掉 source_mode，用户看到的都是“又被重抓了一次”。"""
    from app import jobs

    source = inspect.getsource(jobs.enqueue_draft_regeneration_job)

    assert "source_mode: str = \"auto\"" in source
    assert "auto_illustration_requested, source_mode," in source
