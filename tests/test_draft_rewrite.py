"""审核面板的“重写文案”：用已保存的 README 与证据包重写，而不是只改状态。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import routes


class _StubRepository:
    def __init__(self, *, draft=None, run=None) -> None:
        self.draft = draft
        self.run = run
        self.events: list[tuple[str, str, str]] = []
        self.messages: list[tuple[str, str]] = []
        self.reopened: list[tuple[str, bool, bool]] = []
        self.updated_messages: list[tuple[str, str]] = []
        self.finished: list[tuple[str, str]] = []

    def get_draft(self, draft_id: str):
        return self.draft

    def find_generation_run_for_draft(self, draft_id: str):
        return self.run

    def create_chat_message(self, session_id: str, role: str, content: str):
        self.messages.append((session_id, content))
        return SimpleNamespace(id="message-1", role=role, content=content, created_at="now")

    def reopen_generation_run(self, run, response_message_id: str, auto_review: bool, auto_illustration: bool):
        self.reopened.append((response_message_id, auto_review, auto_illustration))
        run.status = "running"
        return run

    def add_chat_agent_event(self, run_id: str, title: str, detail: str, status: str = "completed", metadata=None):
        self.events.append((title, detail, status))

    def update_chat_message(self, message_id: str, content: str) -> None:
        self.updated_messages.append((message_id, content))

    def finish_chat_agent_run(self, run_id: str, message_id: str, status, summary: str, results, error) -> None:
        self.finished.append((summary, str(error)))


def _draft(status: str = "pending_review", version: int = 3):
    return SimpleNamespace(id="draft-1", status=status, version=version)


def _run():
    return SimpleNamespace(
        id="run-1", session_id="session-1", status="completed",
        auto_review_requested=False, auto_illustration_requested=False,
    )


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def test_rewrite_uses_saved_evidence_and_keeps_draft_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _StubRepository(draft=_draft(), run=_run())
    enqueued: list[tuple] = []

    async def fake_enqueue(settings, run_id, session_id, message_id, draft_id, auto_review, auto_illustration):
        enqueued.append((run_id, session_id, message_id, draft_id, auto_review, auto_illustration))
        return "job-9"

    monkeypatch.setattr(routes, "ContentRepository", lambda session: repository)
    monkeypatch.setattr(routes, "enqueue_draft_regeneration_job", fake_enqueue)
    monkeypatch.setattr(routes, "chat_agent_run_to_dict", lambda run, repo: {"id": run.id})

    result = __import__("asyncio").run(routes.rewrite_draft("draft-1", SimpleNamespace(), _Session()))

    # 复用同一条重生成任务：同一个草稿、原生成记录与原会话，不新建草稿。
    assert enqueued == [("run-1", "session-1", "message-1", "draft-1", False, False)]
    assert repository.reopened == [("message-1", False, False)]
    # 生成记录状态显示“正在重写”，而不是泛化的“重新生成任务已创建”。
    assert repository.events[0][0] == "重写文案"
    assert any(title == "正在重写文案" for title, _detail, _status in repository.events)
    assert repository.run.summary == "正在重写文案"
    assert "已保存的 README 与证据包" in repository.events[0][1]
    assert result["execution"] == {"id": "run-1"}
    assert repository.finished == []


def test_rewrite_rejects_published_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _StubRepository(draft=_draft(status="published"), run=_run())
    monkeypatch.setattr(routes, "ContentRepository", lambda session: repository)

    with pytest.raises(routes.HTTPException) as excinfo:
        __import__("asyncio").run(routes.rewrite_draft("draft-1", SimpleNamespace(), _Session()))

    assert excinfo.value.status_code == 409
    assert "已发布" in str(excinfo.value.detail)


def test_rewrite_requires_origin_generation_run(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _StubRepository(draft=_draft(), run=None)
    monkeypatch.setattr(routes, "ContentRepository", lambda session: repository)

    with pytest.raises(routes.HTTPException) as excinfo:
        __import__("asyncio").run(routes.rewrite_draft("draft-1", SimpleNamespace(), _Session()))

    assert excinfo.value.status_code == 409
    assert "原生成记录" in str(excinfo.value.detail)


def test_rewrite_reports_enqueue_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _StubRepository(draft=_draft(), run=_run())

    async def failing_enqueue(*_args, **_kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(routes, "ContentRepository", lambda session: repository)
    monkeypatch.setattr(routes, "enqueue_draft_regeneration_job", failing_enqueue)

    with pytest.raises(routes.HTTPException) as excinfo:
        __import__("asyncio").run(routes.rewrite_draft("draft-1", SimpleNamespace(), _Session()))

    assert excinfo.value.status_code == 503
    assert repository.updated_messages and "未能入队" in repository.updated_messages[0][1]
    assert repository.finished and repository.finished[0][0] == "重写任务入队失败"
