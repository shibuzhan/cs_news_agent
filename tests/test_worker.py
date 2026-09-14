from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.worker as worker
from app.services import chat_dispatch
from app.domain.models import AgentRunStatus, ConversationIntent


class FakeSession:
    def __init__(self) -> None:
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def commit(self) -> None:
        self.committed = True


class FakeRepository:
    instances: list["FakeRepository"] = []

    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.events: list[dict] = []
        self.finished: dict | None = None
        self.notifications: list[dict] = []
        self.memories: list[dict] = []
        self.messages: list[SimpleNamespace] = []
        self.appended: list[dict] = []
        FakeRepository.instances.append(self)

    def update_chat_message(self, _message_id: str, content: str) -> None:
        self.message = content

    def get_chat_agent_run(self, _run_id: str):
        return SimpleNamespace(
            status=worker.ConversationRunStatus.RUNNING.value,
            intent="general_chat",
            # 受理运行已经挂着一条确定性回执消息；最终回复必须**追加**而不是覆盖它。
            response_message_id="message-receipt",
            summary="正在处理",
        )

    def append_run_reply(self, run_id: str, text: str) -> str:
        row = self.create_chat_message("session-1", "assistant", text)
        self.appended.append({"run_id": run_id, "message_id": row.id})
        return row.id

    def create_chat_message(self, _session_id: str, role: str, content: str):
        row = SimpleNamespace(id=f"message-{len(self.messages) + 1}", role=role, content=content)
        self.messages.append(row)
        return row

    def add_chat_agent_event(self, _run_id: str, title: str, detail: str = "", status: str = "completed", **_kwargs) -> None:
        self.events.append({"sequence": len(self.events) + 1, "title": title, "detail": detail, "status": status})

    def finish_chat_agent_run(self, _run_id: str, _message_id: str, status, summary: str, *_args) -> None:
        self.finished = {"status": status, "summary": summary}

    def upsert_failure_notification(self, source_type: str, source_id: str, category: str, title: str, detail: str, target_view: str) -> None:
        self.notifications.append({"source_type": source_type, "source_id": source_id, "category": category, "title": title, "detail": detail, "target_view": target_view})

    def update_chat_session_memory(self, _session_id: str, **kwargs) -> None:
        self.memories.append(kwargs)


class FakeResult:
    created = 1
    status = AgentRunStatus.SUCCESS
    source_errors: list[dict[str, str]] = []

    def model_dump(self, **_kwargs):
        return {"created": self.created, "runs": []}


def configure_worker_success(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeRepository.instances = []
    monkeypatch.setattr(worker, "Settings", lambda: SimpleNamespace(request_timeout_seconds=1))
    monkeypatch.setattr(worker, "SessionLocal", FakeSession)
    monkeypatch.setattr(worker, "ContentRepository", FakeRepository)
    monkeypatch.setattr(worker, "build_source_tools", lambda *_args: [])
    monkeypatch.setattr(worker, "build_generator", lambda *_args: object())
    async def no_finalize(*_args, **_kwargs) -> str:
        return "finalizer-test"
    monkeypatch.setattr(worker, "enqueue_collection_finalizer", no_finalize)


@pytest.mark.asyncio
async def test_worker_records_completion_after_preexisting_queue_events(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_worker_success(monkeypatch)

    class SuccessfulAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self, _command):
            return FakeResult()

    monkeypatch.setattr(worker, "ContentMainAgent", SuccessfulAgent)

    await worker.process_collection_job({}, "run-1", "session-1", "message-1", ["github"], 10)

    repository = FakeRepository.instances[0]
    assert [event["title"] for event in repository.events] == ["文字生成完成", "未启用自动配图"]
    assert repository.finished is None


@pytest.mark.asyncio
async def test_worker_records_failure_without_reusing_a_fixed_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_worker_success(monkeypatch)

    class FailingAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self, _command):
            raise RuntimeError("采集器测试失败")

    monkeypatch.setattr(worker, "ContentMainAgent", FailingAgent)

    with pytest.raises(RuntimeError, match="采集器测试失败"):
        await worker.process_collection_job({}, "run-1", "session-1", "message-1", ["github"], 10)

    repository = FakeRepository.instances[0]
    assert repository.events[0]["title"] == "文字生成失败"
    assert repository.events[0]["status"] == "failed"
    assert repository.finished["status"].value == "failed"


@pytest.mark.asyncio
async def test_worker_marks_a_handled_source_failure_as_failed_in_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_worker_success(monkeypatch)

    class SourceFailureResult(FakeResult):
        created = 0
        status = AgentRunStatus.FAILED
        source_errors = [{"source": "github", "error": "GitHub Trending 采集失败：All connection attempts failed"}]

        def model_dump(self, **_kwargs):
            return {
                "created": self.created,
                "status": self.status.value,
                "source_errors": self.source_errors,
                "runs": [],
            }

    class SourceFailureAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self, _command):
            return SourceFailureResult()

    monkeypatch.setattr(worker, "ContentMainAgent", SourceFailureAgent)

    await worker.process_collection_job({}, "run-1", "session-1", "message-1", ["github"], 5)

    repository = FakeRepository.instances[0]
    assert repository.message.startswith("采集失败：github 来源请求失败。")
    assert repository.events[0]["status"] == "failed"
    assert repository.finished == {"status": worker.ConversationRunStatus.FAILED, "summary": repository.message}
    assert repository.notifications[0]["category"] == "generation"


@pytest.mark.asyncio
async def test_worker_explains_requested_illustration_has_no_new_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_worker_success(monkeypatch)

    class DuplicateOnlyResult(FakeResult):
        created = 0

        def model_dump(self, **_kwargs):
            return {"created": 0, "runs": [], "source_errors": []}

    class DuplicateOnlyAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self, _command):
            return DuplicateOnlyResult()

    monkeypatch.setattr(worker, "ContentMainAgent", DuplicateOnlyAgent)

    await worker.process_collection_job({}, "run-1", "session-1", "message-1", ["github"], 10, auto_illustration_requested=True)

    repository = FakeRepository.instances[0]
    assert repository.events[-1]["title"] == "未创建图片任务"
    assert "没有新增草稿" in repository.events[-1]["detail"]


@pytest.mark.asyncio
async def test_worker_marks_cancelled_collection_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_worker_success(monkeypatch)

    class CancelledAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self, _command):
            raise __import__("asyncio").CancelledError()

    monkeypatch.setattr(worker, "ContentMainAgent", CancelledAgent)

    with pytest.raises(__import__("asyncio").CancelledError):
        await worker.process_collection_job({}, "run-1", "session-1", "message-1", ["github"], 10)

    repository = FakeRepository.instances[0]
    assert repository.events[0]["title"] == "执行超时"
    assert repository.finished["status"].value == "failed"


@pytest.mark.asyncio
async def test_general_chat_job_persists_the_background_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """受理回执已经写好了；Worker 只把最终回复**追加**为新消息并结束运行。"""
    configure_worker_success(monkeypatch)

    class FakeDeepAgent:
        def __init__(self, _settings) -> None:
            pass

        async def resolve(self, session_id: str, content: str, has_attachment: bool, chat_run_id=None, on_delta=None):
            assert session_id == "session-1"
            assert content == "为什么新增为零"
            assert has_attachment is False
            # 受理运行必须传给 DeepAgent，工具才能复用同一条运行而不是新建卡片。
            assert chat_run_id == "run-1"
            return SimpleNamespace(
                success=True,
                failure_kind=None,
                tools_called=frozenset(),
                decision=SimpleNamespace(
                    intent=ConversationIntent.GENERAL_CHAT,
                    reply="因为候选项目已按去重规则跳过。",
                ),
            )

    monkeypatch.setattr(chat_dispatch, "ContentDeepAgent", FakeDeepAgent)

    await worker.process_general_chat_job({}, "run-1", "session-1", "为什么新增为零")

    repository = FakeRepository.instances[-1]
    assert repository.appended == [{"run_id": "run-1", "message_id": repository.messages[0].id}]
    assert repository.messages[0].content == "因为候选项目已按去重规则跳过。"
    assert repository.finished == {
        "status": worker.ConversationRunStatus.COMPLETED,
        "summary": "因为候选项目已按去重规则跳过。",
    }


def test_collection_completion_marks_zero_drafts_with_model_quota_error_as_failed() -> None:
    result = SimpleNamespace(
        created=0,
        status=AgentRunStatus.PARTIAL,
        source_errors=[],
        runs=[{"errors": [{"error": "Error code: 402 - Insufficient Balance"}]}],
    )

    status, summary, event_status = worker.collection_completion(result)

    assert status == worker.ConversationRunStatus.FAILED
    assert event_status == "failed"
    assert "额度不足" in summary


def test_collection_memory_summary_explains_zero_drafts_without_clearing_context() -> None:
    result = SimpleNamespace(
        created=0,
        runs=[{"duplicates": 1, "skipped": 9}],
    )

    summary = worker.collection_memory_summary(result, "采集完成：新增 0 条待审核草稿")

    assert "已存在未废弃草稿或已发布记录" in summary
    assert "9 条未进入生成候选" in summary


def test_failed_or_timed_out_image_tasks_require_manual_attention() -> None:
    assert worker.image_tasks_need_manual_attention([
        SimpleNamespace(status="completed"), SimpleNamespace(status="failed"),
    ]) is True
    assert worker.image_tasks_need_manual_attention([
        SimpleNamespace(status="timed_out"),
    ]) is True
    assert worker.image_tasks_need_manual_attention([
        SimpleNamespace(status="completed"),
    ]) is False
