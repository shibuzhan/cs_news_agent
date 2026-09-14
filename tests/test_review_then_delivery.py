"""「审核通过就投递」是一条**有条件的两步**，外加一次真实的失败复盘。

真实故障（2026-09-14 21:27，用户截图）：用户先发“仅运行审核”，紧接着发
“审核如果通过就投递草稿箱”。当时的处理链：
1. 第二步的多步计划里，第 1 步（审核）撞上正在跑的审核 → `already_running` 被 `_step_outcome`
   当成**失败** → 整条计划中断，运行直接标红；
2. 第二步的投递要求被丢掉：审核那一次是 `deliver=False`，即使之后审核通过也不会投递；
3. 投递那一步只会说“当前状态不支持”，看不出“因为审核没过”。

现在：投递作为审核的“通过后动作”随任务带上；已有审核在跑时把诉求并进去；
投递单独执行时先回头看状态并说清原因；`already_running` 不再让计划中断。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import chat_dispatch
from app.services.chat_dispatch import ChatDispatchContext
from app.services.chat_receipts import plan_chat_message


class FakeRepository:
    def __init__(self, status: str = "pending_review", active_review=None) -> None:
        self.draft = SimpleNamespace(
            id="draft-1", status=status, version=4, title_options_json=["openai/plugins：示例"],
        )
        self.active_review = active_review
        self.settings: dict[str, str] = {}
        self.events: list[tuple] = []
        self.reviews: list[SimpleNamespace] = []

    # --- 草稿与运行 ---
    def get_draft(self, _draft_id: str):
        return self.draft

    def get_chat_agent_run(self, run_id: str):
        return SimpleNamespace(id=run_id, response_message_id="message-1", session_id="session-1")

    def get_chat_session_memory(self, _session_id: str):
        return SimpleNamespace(active_draft_id=self.draft.id)

    def find_active_auto_review_run(self, _draft_id: str):
        return self.active_review

    def find_active_regeneration_run(self, _draft_id: str, **_kwargs):
        return None

    def count_active_image_jobs(self, _draft_id: str, _statuses) -> int:  # noqa: ANN001
        return 0

    def expire_stale_auto_review_run(self, *_args, **_kwargs) -> None:
        return None

    def create_auto_review_run(self, draft_id: str, _run_id, status: str = "running"):
        row = SimpleNamespace(id=f"review-{len(self.reviews) + 1}", draft_id=draft_id, status=status)
        self.reviews.append(row)
        return row

    def get_wechat_publication_for_draft(self, _draft_id: str):
        return None

    def get_app_setting(self, key: str, default=None):  # noqa: ANN001
        return self.settings.get(key, default)

    def set_app_setting(self, key: str, value: str, **_kwargs) -> None:
        self.settings[key] = value

    def add_chat_agent_event(self, *_args, **_kwargs):
        # 必须返回事件行：`_dispatch_plan` 用 `None` 表示“运行已被删除”，会直接收尾。
        self.events.append(_args)
        return SimpleNamespace(id=f"event-{len(self.events)}")

    def create_chat_message(self, _session_id: str, _role: str, content: str):
        return SimpleNamespace(id="message-anchor", content=content)

    def update_chat_session_memory(self, *_args, **_kwargs) -> None:
        return None


class FakeSession:
    def commit(self) -> None:
        return None

    def flush(self) -> None:
        return None


def make_context(repository: FakeRepository) -> ChatDispatchContext:
    return ChatDispatchContext(
        settings=SimpleNamespace(auto_wechat_draft_enabled=True, collection_job_timeout_seconds=900),
        repository=repository,
        session=FakeSession(),
        session_id="session-1",
        run_id="run-1",
        content="审核如果通过就投递草稿箱",
    )


def test_the_phrase_plans_review_then_delivery() -> None:
    plan = plan_chat_message("审核如果通过就投递草稿箱")

    assert plan.steps == ("review", "deliver")
    assert plan.multi is True


def test_plan_hands_delivery_to_the_review_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """投递意图必须随审核任务带上，并且不再另起一个投递任务。"""
    repository = FakeRepository()
    ctx = make_context(repository)
    captured: dict = {}

    async def fake_review(_ctx, *, deliver: bool = False):  # noqa: ANN001
        captured["deliver"] = deliver
        return {"status": "started", "status_text": "审核中", "message": "已开始审核"}

    async def fake_deliver(*_args, **_kwargs):  # noqa: ANN002, ANN003
        pytest.fail("投递意图已随审核带上，不应再单独投递")

    monkeypatch.setattr(chat_dispatch, "_step_review", fake_review)
    monkeypatch.setattr(chat_dispatch, "_step_deliver", fake_deliver)

    outcome = asyncio.run(chat_dispatch._dispatch_plan(ctx, plan_chat_message(ctx.content)))

    assert captured["deliver"] is True, "审核任务必须带上 deliver=True"
    assert outcome.keep_running is True, "审核还在跑：运行要保持处理中，由审核任务收尾"


def test_review_already_running_no_longer_breaks_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """`already_running` 是“有人接管”，不是失败——旧实现让它整条计划中断并标红。"""
    repository = FakeRepository(active_review=SimpleNamespace(id="review-active", status="running"))
    ctx = make_context(repository)

    async def fake_deliver(*_args, **_kwargs):  # noqa: ANN002, ANN003
        pytest.fail("不应再单独投递")

    monkeypatch.setattr(chat_dispatch, "_step_deliver", fake_deliver)

    outcome = asyncio.run(chat_dispatch._dispatch_plan(ctx, plan_chat_message(ctx.content)))

    assert outcome.keep_running is True
    review_fact = [item for item in outcome.results if item.get("step") == "review"][0]
    assert review_fact["status"] == "already_running"
    # 投递要求被并进那次正在跑的审核里（标记里 deliver=True）。
    marker = repository.settings["pending.auto_review.draft-1"]
    assert '"deliver": true' in marker
    assert review_fact["merged_deliver"] is True


def test_merging_delivery_into_an_active_review_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent_tools import draft_actions

    repository = FakeRepository(active_review=SimpleNamespace(id="review-active", status="running", chat_agent_run_id="run-9"))
    monkeypatch.setattr(
        draft_actions, "enqueue_auto_review_job", lambda *_a, **_k: pytest.fail("不得新建审核")
    )

    result = asyncio.run(
        draft_actions._start_review(
            SimpleNamespace(collection_job_timeout_seconds=900), repository, "session-1",
            repository.draft, deliver=True, chat_run_id="run-1",
        )
    )

    assert result["status"] == "already_running"
    assert result["merged_deliver"] is True
    assert "通过后会自动投递" in result["message"]
    assert '"deliver": true' in repository.settings["pending.auto_review.draft-1"]
    assert repository.reviews == [], "合并不得新建审核记录"


def test_deliver_only_after_review_says_why_it_did_not_deliver(monkeypatch: pytest.MonkeyPatch) -> None:
    """审核没过时投递必须说清原因，而不是“当前状态不支持”。"""
    repository = FakeRepository(status="needs_revision")
    ctx = make_context(repository)

    result = asyncio.run(chat_dispatch._step_deliver(ctx, after_review=True))

    assert result["status"] == "rejected"
    assert "审核没有通过" in result["message"]
    assert "needs_revision" in result["message"]


def test_manual_deliver_is_told_to_pass_review_first(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository(status="pending_review")
    ctx = make_context(repository)

    result = asyncio.run(chat_dispatch._step_deliver(ctx))

    assert result["status"] == "rejected"
    assert "只有**审核通过**的文章才能投递" in result["message"]


def test_delivery_tool_explains_why_it_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """`publish_to_wechat_draft` 工具过去只会回“当前状态不支持这个操作”。"""
    from app.agent_tools import draft_actions

    repository = FakeRepository(status="pending_review")
    message = draft_actions._not_ready_for_delivery(repository, repository.draft)

    assert "只有**审核通过**的文章才能投递" in message
    assert "先帮你审核" in message
    assert "当前状态（pending_review）不支持" not in message


def test_delivery_tool_names_the_running_review() -> None:
    """审核在跑时投递要说清“等它通过”，并给出“通过就投递”这条下一步。"""
    from app.agent_tools import draft_actions

    repository = FakeRepository(active_review=SimpleNamespace(id="review-active", status="running"))
    message = draft_actions._not_ready_for_delivery(repository, repository.draft)

    assert "审核还在处理中" in message
    assert "通过就投递" in message


def test_delivery_tool_names_the_running_rewrite() -> None:
    from app.agent_tools import draft_actions

    repository = FakeRepository(status="pending_review")
    repo_draft = repository.draft
    message = draft_actions._not_ready_for_delivery(repository, repo_draft)

    assert "审核通过" in message  # 没有在跑的任务时，明确要求先审核
