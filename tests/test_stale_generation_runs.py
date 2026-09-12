from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

# 回归测试只构造 SQLAlchemy 查询对象，不连接项目 PostgreSQL。
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.config import Settings
from app.domain.models import ConversationIntent, ConversationRunStatus
from app.storage.repositories import ContentRepository


class FakeScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.flushed = False
        self.statement = None

    def scalars(self, statement):
        self.statement = statement
        return FakeScalarRows(self.rows)

    def flush(self):
        self.flushed = True


def test_stale_generation_run_is_failed_without_deleting_related_content(monkeypatch) -> None:
    row = SimpleNamespace(
        id="run-1",
        status=ConversationRunStatus.RUNNING.value,
        summary="正在生成",
        error_message=None,
        attempt_started_at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
        finished_at=None,
        response_message_id="message-1",
    )
    session = FakeSession([row])
    repository = ContentRepository(session)
    events: list[dict] = []
    notifications: list[dict] = []
    message_updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        repository,
        "add_chat_agent_event",
        lambda run_id, title, detail, status, metadata: events.append(
            {"run_id": run_id, "title": title, "detail": detail, "status": status, "metadata": metadata}
        ),
    )
    monkeypatch.setattr(
        repository,
        "upsert_failure_notification",
        lambda source_type, source_id, category, title, detail, target_view: notifications.append(
            {"source_type": source_type, "source_id": source_id, "category": category, "title": title, "detail": detail, "target_view": target_view}
        ),
    )
    monkeypatch.setattr(
        repository,
        "update_chat_message",
        lambda message_id, detail: message_updates.append((message_id, detail)),
    )

    reconciled = repository.reconcile_stale_generation_runs(1)

    assert reconciled == [row]
    assert row.status == ConversationRunStatus.FAILED.value
    assert row.summary == "生成任务未完成"
    assert row.finished_at is not None
    assert message_updates == [("message-1", row.error_message)]
    assert events[0]["metadata"]["state"] == "stale_failed"
    assert notifications[0]["category"] == "generation"
    assert session.flushed is True
    assert "chat_agent_runs.attempt_started_at <=" in str(session.statement)


def test_reopening_generation_record_refreshes_attempt_start_without_changing_creation_time(monkeypatch) -> None:
    first_created_at = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    retry_started_at = datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        status=ConversationRunStatus.FAILED.value,
        summary="生成失败",
        error_message="旧错误",
        created_at=first_created_at,
        attempt_started_at=first_created_at,
        finished_at=first_created_at,
        response_message_id="old-message",
        auto_review_requested=False,
        auto_illustration_requested=False,
    )
    session = FakeSession([])
    repository = ContentRepository(session)
    monkeypatch.setattr("app.storage.repositories.utcnow", lambda: retry_started_at)

    repository.reopen_generation_run(row, "new-message", True, True)

    assert row.status == ConversationRunStatus.RUNNING.value
    assert row.created_at == first_created_at
    assert row.attempt_started_at == retry_started_at
    assert row.finished_at is None
    assert row.response_message_id == "new-message"
    assert row.auto_review_requested is True
    assert row.auto_illustration_requested is True
    assert session.flushed is True


def test_stale_timeout_covers_the_longest_background_task_plus_buffer() -> None:
    settings = Settings(
        collection_job_timeout_seconds=600,
        image_generation_job_timeout_seconds=1200,
    )

    assert settings.generation_run_stale_timeout_seconds == 1500


def test_delete_generation_run_audit_keeps_business_draft_data(monkeypatch) -> None:
    row = SimpleNamespace(
        id="run-2",
        intent=ConversationIntent.COLLECT_NEWS.value,
        status=ConversationRunStatus.FAILED.value,
    )

    class DeleteSession:
        def __init__(self):
            self.deleted: list[object] = []
            self.flushed = False

        def get(self, _model, _run_id):
            return row

        def scalars(self, _statement):
            return FakeScalarRows([])

        def delete(self, value):
            self.deleted.append(value)

        def flush(self):
            self.flushed = True

    session = DeleteSession()
    repository = ContentRepository(session)

    deleted_id = repository.delete_generation_run_audit("run-2")

    assert deleted_id == "run-2"
    assert session.deleted == [row]
    assert session.flushed is True
