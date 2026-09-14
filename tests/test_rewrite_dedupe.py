"""重写去重与草稿版本写入保护。

真实故障（2026-09-14 09:52，用户报“聊天框异常”）：
同一条消息“openai/plugins重新写稿”被**两条路径各入队一次**重写——模型直接调 `rewrite_draft`
工具入队一次，意图兜底（`_step_rewrite`）又入队一次。两个任务并发写同一草稿：
- 第一个写完 v2（改稿又写了 v3），第二个拿着自己读到的旧版本号插 v2 →
  `uq_draft_revision_version` 唯一冲突 → 整个任务以 IntegrityError 失败；
- 内容其实已经写进去了，用户却看到“生成失败”，草稿版本与报告对不上。

两条防线：①入队前按 `target_draft_id` 去重；②落库时版本号被抢先就换号重写一次。
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.agent_tools import draft_actions
from app.agent_tools.draft_actions import start_draft_rewrite
from app.domain.models import DraftContent
from app.workflows import content_workflow
from app.workflows.content_workflow import ContentPipeline


def version_conflict() -> IntegrityError:
    return IntegrityError("INSERT INTO draft_revisions ...", {}, Exception("uq_draft_revision_version"))


class FakeRepository:
    """只实现 `_start_rewrite` 与去重判断用到的那几个方法。"""

    def __init__(self, *, active_run=None, claim_ok: bool = True, rewrite_job_id=None) -> None:
        self.active_run = active_run
        self.claim_ok = claim_ok
        self.claimed: list[tuple[str, str]] = []
        self.marked: list[tuple[str, str, str]] = []
        self.queries: list[dict] = []
        self.enqueued: list[dict] = []
        self.events: list[tuple] = []
        self.messages: list[SimpleNamespace] = []
        self.finished: list[dict] = []
        self.intake_run = SimpleNamespace(
            id="run-intake", status="running", response_message_id="message-receipt",
            auto_review_requested=False, auto_illustration_requested=False,
            rewrite_job_id=rewrite_job_id, target_draft_id=None,
        )

    def get_chat_agent_run(self, run_id: str):
        if run_id == self.intake_run.id:
            return self.intake_run
        raise AssertionError(f"未知运行：{run_id}")

    def find_generation_run_for_draft(self, _draft_id: str):
        return None  # 老草稿：没有原生成记录

    def claim_run_target_draft(self, run_id: str, draft_id: str) -> bool:
        self.claimed.append((run_id, draft_id))
        return self.claim_ok

    def mark_rewrite_job_enqueued(self, run_id: str, draft_id: str, job_id: str) -> None:
        self.marked.append((run_id, draft_id, job_id))

    def find_active_regeneration_run(self, draft_id: str, **kwargs):  # noqa: ANN003
        self.queries.append({"draft_id": draft_id, **kwargs})
        return self.active_run

    def create_chat_message(self, session_id: str, role: str, content: str):
        row = SimpleNamespace(id=f"message-{len(self.messages) + 1}", content=content)
        self.messages.append(row)
        return row

    def add_chat_agent_event(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.events.append((args, kwargs))

    def finish_chat_agent_run(self, run_id, message_id, status, summary, *_args) -> None:  # noqa: ANN001
        self.finished.append({"run_id": run_id, "status": status})

    @property
    def session(self):  # noqa: ANN201
        return SimpleNamespace(flush=lambda: None)


@pytest.fixture()
def draft() -> SimpleNamespace:
    return SimpleNamespace(id="draft-1", version=1, title_options_json=["openai/plugins：示例"])


def _settings() -> SimpleNamespace:
    return SimpleNamespace(collection_job_timeout_seconds=900)


def test_second_rewrite_request_on_the_same_message_is_not_enqueued_again(
    monkeypatch: pytest.MonkeyPatch, draft
) -> None:
    """同一条消息的第二条执行路径必须复用已有任务，而不是再入队一次。

    真实故障路径：模型调 `rewrite_draft` 工具入队一次，意图兜底（`_step_rewrite`）又入队一次；
    两次用的是**同一个运行**，所以只有“本次运行已经入队过”这个标记能认出来。
    """
    repository = FakeRepository(active_run=None, rewrite_job_id="job-already")
    monkeypatch.setattr(draft_actions, "enqueue_draft_regeneration_job", lambda *_a, **_k: pytest.fail("不得重复入队"))

    result = asyncio.run(
        start_draft_rewrite(_settings(), repository, "session-1", draft, chat_run_id="run-intake")
    )

    assert result["status"] == "already_running"
    assert result["run_id"] == "run-intake"
    assert "已经在重写中" in result["message"]
    # 第二次请求不能再记“正在重写”事件/消息，否则对话里会多出一条永远不更新的卡片。
    assert repository.events == []
    assert repository.messages == []
    assert repository.queries == [], "同一条请求已经入队过，不必再查其它运行"


def test_rewrite_is_skipped_when_another_run_owns_the_draft(monkeypatch: pytest.MonkeyPatch, draft) -> None:
    repository = FakeRepository(active_run=SimpleNamespace(id="run-active"))
    monkeypatch.setattr(draft_actions, "enqueue_draft_regeneration_job", lambda *_a, **_k: pytest.fail("不得重复入队"))

    result = asyncio.run(
        start_draft_rewrite(_settings(), repository, "session-1", draft, chat_run_id="run-intake")
    )

    assert result["status"] == "already_running"
    assert result["run_id"] == "run-active"
    assert repository.events == [] and repository.messages == []
    assert repository.claimed == [("run-intake", "draft-1")], "先登记本次运行的目标草稿，去重判断才成立"
    assert repository.queries[0]["exclude_run_id"] == "run-intake"
    assert "session_id" not in repository.queries[0], "去重必须按草稿判断，跨会话也要拦住"


def test_rewrite_is_enqueued_when_nothing_is_running(monkeypatch: pytest.MonkeyPatch, draft) -> None:
    repository = FakeRepository(active_run=None)
    enqueued: list[dict] = []

    async def fake_enqueue(_settings, run_id, session_id, message_id, draft_id, *_args):  # noqa: ANN001
        enqueued.append({"run_id": run_id, "draft_id": draft_id})
        return "job-1"

    monkeypatch.setattr(draft_actions, "enqueue_draft_regeneration_job", fake_enqueue)

    result = asyncio.run(
        start_draft_rewrite(_settings(), repository, "session-1", draft, chat_run_id="run-intake")
    )

    assert result["status"] == "started"
    assert enqueued == [{"run_id": "run-intake", "draft_id": "draft-1"}]
    assert repository.claimed == [("run-intake", "draft-1")]
    assert repository.marked == [("run-intake", "draft-1", "job-1")], "入队后必须登记任务号，第二次请求才认得出来"


def test_claim_refuses_when_the_run_already_targets_another_draft() -> None:
    """一条运行只服务一篇草稿：指向别的草稿时不能覆盖登记。"""
    from app.storage.repositories import ContentRepository

    class Row:
        target_draft_id = "draft-other"

    repository = ContentRepository.__new__(ContentRepository)
    repository.session = SimpleNamespace(flush=lambda: None, get=lambda *_a, **_k: Row())  # type: ignore[assignment]

    assert repository.claim_run_target_draft("run-1", "draft-1") is False
    assert Row.target_draft_id == "draft-other"


def test_worker_skips_a_superseded_regeneration_job() -> None:
    """第二道防线：任务开跑时发现草稿已被别的运行占用，就干净收尾而不是并发改写。"""
    from app import worker

    source = inspect.getsource(worker.process_draft_regeneration_job)

    assert "find_active_regeneration_run" in source
    assert "draft_regeneration_superseded" in source
    assert "release_run_target_draft" in source, "跑完必须放开登记，否则这篇草稿再也重写不了"


def test_repository_releases_the_draft_when_a_run_finishes() -> None:
    from app.storage.repositories import ContentRepository

    source = inspect.getsource(ContentRepository.finish_chat_agent_run)

    assert "row.target_draft_id = None" in source
    assert "row.rewrite_job_id = None" in source, "任务号也要清掉，否则这条运行再也发不起新的重写"


def test_dedupe_query_skips_orphan_runs() -> None:
    """服务重启会留下永远 running 的记录：去重不能把它们当成“真的在跑”，否则草稿被锁死。"""
    from app.storage.repositories import ContentRepository

    source = inspect.getsource(ContentRepository.find_active_regeneration_run)

    assert "regeneration_run_expired" in source
    assert "attempt_started_at or row.created_at" in source
    assert "exclude_run_id" in source


def sample_content() -> DraftContent:
    return DraftContent(
        title_options=["t"], summary_cn="s", body="正文",
        source_name="GitHub", source_url="https://github.com/openai/plugins",
    )


def test_version_conflict_rewrites_with_the_next_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """版本号被抢先时必须换号重写，不能让 9–12 分钟的生成结果白白丢掉。"""
    calls: list[int] = []

    class Repository:
        def save_source(self, _item):
            return SimpleNamespace(row=SimpleNamespace(id="source-1"))

        def regenerate_draft(self, draft_id: str, _content, _evidence):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise version_conflict()
            return SimpleNamespace(id=draft_id, version=2)

    class Session:
        def begin_nested(self):  # noqa: ANN201
            class Savepoint:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            return Savepoint()

    pipeline = ContentPipeline.__new__(ContentPipeline)
    pipeline.repository = Repository()  # type: ignore[assignment]
    pipeline.session = Session()  # type: ignore[assignment]
    pipeline.source_snapshots = None

    draft = pipeline._write_regenerated_draft("draft-1", sample_content(), [])

    assert calls == [1, 2], "第一次冲突后必须重试一次"
    assert draft.version == 2


def test_version_conflict_gives_up_after_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    class Repository:
        def regenerate_draft(self, *_args, **_kwargs):
            raise version_conflict()

    class Session:
        def begin_nested(self):  # noqa: ANN201
            class Savepoint:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            return Savepoint()

    pipeline = ContentPipeline.__new__(ContentPipeline)
    pipeline.repository = Repository()  # type: ignore[assignment]
    pipeline.session = Session()  # type: ignore[assignment]

    with pytest.raises(IntegrityError):
        pipeline._write_regenerated_draft("draft-1", sample_content(), [])


def test_pipeline_uses_the_guarded_write_path() -> None:
    source = inspect.getsource(content_workflow.ContentPipeline.regenerate_draft)

    assert "_write_regenerated_draft" in source, "重生成必须走带版本保护与重试的写入路径"
