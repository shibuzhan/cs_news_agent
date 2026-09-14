"""长任务串行化与联网检索可见性。

三个真实问题合成一个文件：

1. **审核撞在跑的重写**（2026-09-14 09:53 实测）：重写要 9–12 分钟，用户紧接着说“再跑一轮审核”，
   审核立刻开始，审的是**马上要被替换掉的旧版本**；随后审核内的自动改稿与重写并发写同一草稿，
   后完成的那次撞上 `uq_draft_revision_version` → 用户看到“生成失败”，而正文其实已经改了。
2. **联网检索看不见**：审核里确实调了 Exa（日志有 `exa_mcp_call_finished`），但汇报里不提，
   界面上也没有“联网补充”标记，用户只能怀疑“搜索没被使用”。
3. **联网证据会被重写丢掉**：`regenerate_draft` 原本整体替换 `evidence_json`。
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from app.agent_tools import draft_actions
from app.services import pending_reviews
from app.storage.repositories import ContentRepository


class FakeRepository:
    def __init__(
        self, *, active_rewrite=None, active_review=None, active_images: int = 0, pending: dict | None = None,
    ) -> None:
        self.active_rewrite = active_rewrite
        self.active_review = active_review
        self.active_images = active_images
        self.pending = pending
        self.marked_pending: list[dict] = []
        self.events: list[tuple] = []
        self.messages: list[SimpleNamespace] = []
        self.created_runs: list[SimpleNamespace] = []
        self.reviews: list[SimpleNamespace] = []
        self.enqueued_reviews: list[dict] = []
        self.intake_run = SimpleNamespace(
            id="run-intake", status="running", response_message_id="message-receipt",
            auto_review_requested=False, auto_illustration_requested=False, rewrite_job_id=None,
        )

    def get_chat_agent_run(self, run_id: str):
        if run_id == self.intake_run.id:
            return self.intake_run
        raise AssertionError(f"未知运行：{run_id}")

    def create_chat_agent_run(self, session_id, request_message_id, intent, *_a):  # noqa: ANN001
        run = SimpleNamespace(
            id=f"run-{len(self.created_runs) + 1}", session_id=session_id,
            intent=getattr(intent, "value", str(intent)), status="running",
            response_message_id=None, auto_review_requested=False,
            auto_illustration_requested=False, rewrite_job_id=None,
        )
        self.created_runs.append(run)
        return run

    def create_chat_message(self, session_id: str, role: str, content: str):
        row = SimpleNamespace(id=f"message-{len(self.messages) + 1}", content=content)
        self.messages.append(row)
        return row

    def add_chat_agent_event(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.events.append((args, kwargs))

    def expire_stale_auto_review_run(self, *_a, **_k) -> None:
        return None

    def find_active_auto_review_run(self, _draft_id: str):
        return self.active_review

    def count_active_image_jobs(self, _draft_id: str, _statuses) -> int:  # noqa: ANN001
        return self.active_images

    def find_active_regeneration_run(self, _draft_id: str, **_kwargs):
        return self.active_rewrite

    def create_auto_review_run(self, draft_id: str, _run_id, status: str = "running"):
        row = SimpleNamespace(id=f"review-{len(self.reviews) + 1}", draft_id=draft_id, status=status)
        self.reviews.append(row)
        return row

    def set_app_setting(self, key: str, value: str, **_kwargs) -> None:
        self.marked_pending.append({"key": key, "value": value})

    def get_app_setting(self, _key: str):
        return None


@pytest.fixture()
def draft() -> SimpleNamespace:
    return SimpleNamespace(id="draft-1", version=3, title_options_json=["openai/plugins：示例"], status="pending_review")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(collection_job_timeout_seconds=900)


def test_review_waits_for_an_in_flight_rewrite(monkeypatch: pytest.MonkeyPatch, draft) -> None:
    """重写在跑时审核必须排队：审旧版本 + 并发改稿是那条“生成失败”的根因。"""
    repository = FakeRepository(active_rewrite=SimpleNamespace(id="run-rewriting", status="running"))
    monkeypatch.setattr(draft_actions, "enqueue_auto_review_job", lambda *_a, **_k: pytest.fail("不得立刻审核"))

    result = asyncio.run(
        draft_actions._start_review(
            _settings(), repository, "session-1", draft, deliver=False,
            chat_run_id="run-intake", revise=True,
        )
    )

    assert result["status"] == "queued_after_rewrite"
    assert result["pending_rewrite_run_id"] == "run-rewriting"
    assert "新版本" in result["message"]
    assert [item["key"] for item in repository.marked_pending] == [pending_reviews.pending_key("draft-1")]
    assert '"revise": true' in repository.marked_pending[0]["value"], "排队不能把“只审不改/改稿一轮”的口径弄丢"
    assert repository.reviews == [], "排队不应创建审核记录"


def test_review_still_queues_behind_images(draft) -> None:
    """配图等待逻辑不能被重写等待挤掉。"""
    repository = FakeRepository(active_images=2)

    result = asyncio.run(
        draft_actions._start_review(
            _settings(), repository, "session-1", draft, deliver=False, chat_run_id="run-intake",
        )
    )

    assert result["status"] == "queued_after_images"
    assert result["pending_images"] == 2


def test_review_starts_when_nothing_is_running(monkeypatch: pytest.MonkeyPatch, draft) -> None:
    repository = FakeRepository()
    enqueued: list[dict] = []

    async def fake_enqueue(_settings, draft_id, review_id, deliver, chat_run_id, revise=True):  # noqa: ANN001
        enqueued.append({"draft_id": draft_id, "deliver": deliver, "revise": revise})
        return "job-1"

    monkeypatch.setattr(draft_actions, "enqueue_auto_review_job", fake_enqueue)

    result = asyncio.run(
        draft_actions._start_review(
            _settings(), repository, "session-1", draft, deliver=False,
            chat_run_id="run-intake", revise=False,
        )
    )

    assert result["status"] == "started"
    assert enqueued == [{"draft_id": "draft-1", "deliver": False, "revise": False}]


def _patch_repository(monkeypatch: pytest.MonkeyPatch, repository: FakeRepository) -> None:
    """工具内部 `with SessionLocal() as session: ContentRepository(session)`：两处都要换掉。"""
    monkeypatch.setattr(draft_actions, "SessionLocal", lambda: _FakeSession(repository))
    monkeypatch.setattr(draft_actions, "ContentRepository", lambda _session: repository)
    monkeypatch.setattr(draft_actions, "get_settings", _settings)


def test_apply_revision_refuses_while_a_rewrite_is_running(
    monkeypatch: pytest.MonkeyPatch, draft
) -> None:
    """正文正在重写时不再并发改稿：两个写者抢版本号只会让其中一个失败。"""
    repository = FakeRepository(active_rewrite=SimpleNamespace(id="run-rewriting"))
    _patch_repository(monkeypatch, repository)
    monkeypatch.setattr(draft_actions, "_resolve_draft", lambda *_a, **_k: (draft, ""))

    tools = {item.name: item for item in draft_actions.build_draft_action_tools("session-1")}
    result = tools["apply_revision_issues"].invoke({"extra_issues": ["把第 3 段重复删掉"]})

    assert result["status"] == "rejected"
    assert "正在重写" in result["message"]
    assert repository.events == [], "被拒绝时不该留下“改稿已入队”的事件"


def test_pending_review_marker_carries_revise_and_launches_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """排队标记要带上 revise：用户点“只审不改”，排队后也不能变成“改稿一轮”。"""
    stored: dict[str, str] = {}

    class Repo:
        def set_app_setting(self, key, value, **_kwargs):  # noqa: ANN001
            stored[key] = value

        def get_app_setting(self, key):  # noqa: ANN001
            return stored.get(key)

        def create_auto_review_run(self, draft_id, _chat, status="running"):  # noqa: ANN001
            return SimpleNamespace(id="review-1", draft_id=draft_id, status=status)

        def expire_stale_auto_review_run(self, *_a, **_k) -> None:
            return None

        def add_chat_agent_event(self, *_a, **_k) -> None:
            return None

    enqueued: list[dict] = []

    async def fake_enqueue(_settings, draft_id, review_id, deliver, chat_run_id, revise=True):  # noqa: ANN001
        enqueued.append({"draft_id": draft_id, "revise": revise})
        return "job-1"

    monkeypatch.setattr("app.jobs.enqueue_auto_review_job", fake_enqueue)
    repository = Repo()
    pending_reviews.mark_pending_review(repository, "draft-1", deliver=False, chat_run_id="run-1", revise=False)

    launched = asyncio.run(pending_reviews.launch_pending_reviews(_settings(), repository, ["draft-1"]))

    assert launched == ["draft-1"]
    assert enqueued == [{"draft_id": "draft-1", "revise": False}]
    assert pending_reviews.read_pending_review(repository, "draft-1") is None, "发起后要清掉标记"


def test_auto_revision_defers_to_a_running_rewrite() -> None:
    from app.services import auto_delivery

    source = inspect.getsource(auto_delivery.auto_review_and_create_wechat_draft)

    assert "find_active_regeneration_run" in source
    assert "revision_deferred" in source
    assert "reviewed_while_rewriting" in source


def test_search_facts_are_reported_to_the_user() -> None:
    """联网检索必须出现在汇报里：否则用户只能猜“搜索到底用没用”。"""
    from app import worker

    source = inspect.getsource(worker._report_auto_review_result)

    assert "searches" in source and "web_search" in source
    assert "本轮没有联网检索" in source


def test_search_evidence_survives_regeneration() -> None:
    """重写只替换来源条目，联网补充必须保留（否则花掉的检索白费）。"""
    source = inspect.getsource(ContentRepository._merge_regenerated_evidence)
    regenerate = inspect.getsource(ContentRepository.regenerate_draft)

    assert "_merge_regenerated_evidence" in regenerate
    assert 'str(item.get("origin") or "").endswith("search")' in source


def test_rewrite_job_launches_the_queued_review() -> None:
    from app import worker

    source = inspect.getsource(worker.process_draft_regeneration_job)

    assert "launch_pending_reviews" in source
    assert "regeneration_launched_pending_review" in source
    # 有新配图时必须交给收束逻辑：审核要等配图，否则又变成“审核早于配图”。
    assert "regeneration_defers_pending_review" in source


def test_queue_tool_reports_active_and_queued_work(
    monkeypatch: pytest.MonkeyPatch, draft
) -> None:
    """Agent 要能先查队列：在跑什么、有没有排队中的审核。"""
    repository = FakeRepository(
        active_rewrite=SimpleNamespace(id="run-rewriting", attempt_started_at=None, created_at=None),
        active_review=SimpleNamespace(id="review-1", status="running"),
        active_images=1,
    )
    _patch_repository(monkeypatch, repository)
    monkeypatch.setattr(draft_actions, "_resolve_draft", lambda *_a, **_k: (draft, ""))
    monkeypatch.setattr(pending_reviews, "read_pending_review", lambda *_a, **_k: {"deliver": False, "revise": True})

    tools = {item.name: item for item in draft_actions.build_draft_action_tools("session-1")}
    result = tools["list_active_tasks"].invoke({})

    assert result["status"] == "ok"
    assert result["busy"] is True
    assert {item["task"] for item in result["active_tasks"]} == {"重写正文", "自动审核", "生成配图"}
    assert "排队" in result["message"]


class _FakeSession:
    """`with SessionLocal() as session` 里只需要能进能出。"""

    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None
