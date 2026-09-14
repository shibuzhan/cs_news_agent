"""重写不再要求“必须有原生成记录”：老草稿只要还有来源快照就能重写。

真实故障（用户截图）：点“重写文案”后被拒“这篇缺少可回溯的原生成记录，无法按已保存证据重写。”
原因：重写把报告锚点写回**原生成记录**，因此把“找得到原记录”当成了硬前提；而更早生成的草稿
没有 collect_news 运行审计（draft_ids 是后加的字段），于是永远重写不了。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agent_tools import draft_actions
from app.agent_tools.draft_actions import start_draft_rewrite
from app.domain.models import ConversationRunStatus
from app.services import chat_dispatch


class FakeRepository:
    def __init__(self, *, intake_run: SimpleNamespace | None = None) -> None:
        self.intake_run = intake_run
        self.events: list[tuple] = []
        self.finished: list[dict] = []
        self.enqueued: list[dict] = []
        self.created_runs: list[SimpleNamespace] = []
        self.messages: list[SimpleNamespace] = []

    # --- 运行 ---
    def get_chat_agent_run(self, run_id: str):
        if self.intake_run is not None and run_id == self.intake_run.id:
            return self.intake_run
        for run in self.created_runs:
            if run.id == run_id:
                return run
        raise AssertionError(f"未知运行：{run_id}")

    def create_chat_agent_run(self, session_id: str, request_message_id, intent, *_args):  # noqa: ANN001
        run = SimpleNamespace(
            id=f"run-{len(self.created_runs) + 1}",
            session_id=session_id,
            intent=getattr(intent, "value", str(intent)),
            status=ConversationRunStatus.RUNNING.value,
            response_message_id=None,
            auto_review_requested=False,
            auto_illustration_requested=False,
        )
        self.created_runs.append(run)
        return run

    def create_chat_message(self, session_id: str, role: str, content: str):
        row = SimpleNamespace(id=f"message-{len(self.messages) + 1}", session_id=session_id, role=role, content=content)
        self.messages.append(row)
        return row

    def add_chat_agent_event(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.events.append((args, kwargs))

    def finish_chat_agent_run(self, run_id, message_id, status, summary, *_args) -> None:  # noqa: ANN001
        self.finished.append(
            {"run_id": run_id, "message_id": message_id, "status": status, "summary": summary}
        )

    def find_generation_run_for_draft(self, _draft_id: str):
        return None  # 老草稿：没有原生成记录

    def reopen_generation_run(self, *_args, **_kwargs) -> None:
        raise AssertionError("没有原生成记录时不应尝试重开它")

    @property
    def session(self):  # noqa: ANN201
        return SimpleNamespace(flush=lambda: None)


@pytest.fixture()
def draft() -> SimpleNamespace:
    return SimpleNamespace(id="draft-1", version=1, title_options_json=["openai/plugins：示例"])


def test_rewrite_without_origin_run_uses_the_intake_run(monkeypatch: pytest.MonkeyPatch, draft) -> None:  # noqa: ANN001
    intake = SimpleNamespace(
        id="run-intake",
        status=ConversationRunStatus.RUNNING.value,
        response_message_id="message-receipt",
        auto_review_requested=False,
        auto_illustration_requested=False,
        intent="regenerate_draft",
    )
    repository = FakeRepository(intake_run=intake)

    async def fake_enqueue(_settings, run_id, session_id, message_id, draft_id, *_args):  # noqa: ANN001
        repository.enqueued.append(
            {"run_id": run_id, "message_id": message_id, "draft_id": draft_id}
        )
        return "job-1"

    monkeypatch.setattr(draft_actions, "enqueue_draft_regeneration_job", fake_enqueue)

    result = asyncio.run(
        start_draft_rewrite(
            SimpleNamespace(), repository, "session-1", draft, chat_run_id="run-intake"
        )
    )

    assert result["status"] == "started"
    assert result["run_id"] == "run-intake", "报告落在受理运行上，用户看到的还是同一张卡片"
    assert repository.enqueued == [
        {"run_id": "run-intake", "message_id": "message-receipt", "draft_id": "draft-1"}
    ]
    # 受理运行**不能**在这里结束：重写任务还要在同一张卡片上汇报。
    assert repository.finished == []
    details = [event[0][2] for event in repository.events]
    assert any("使用已保存的 README 与证据包重写" in detail for detail in details)


def test_rewrite_without_origin_run_and_without_chat_run_creates_an_anchor(
    monkeypatch: pytest.MonkeyPatch, draft
) -> None:  # noqa: ANN001
    repository = FakeRepository()

    async def fake_enqueue(_settings, run_id, session_id, message_id, draft_id, *_args):  # noqa: ANN001
        repository.enqueued.append({"run_id": run_id, "message_id": message_id})
        return "job-2"

    monkeypatch.setattr(draft_actions, "enqueue_draft_regeneration_job", fake_enqueue)

    result = asyncio.run(start_draft_rewrite(SimpleNamespace(), repository, "session-1", draft))

    created = repository.created_runs[0]
    assert result["run_id"] == created.id
    assert created.response_message_id == repository.messages[0].id, "独立运行也要有可追加汇报的锚点"
    assert repository.enqueued == [{"run_id": created.id, "message_id": repository.messages[0].id}]


def test_dispatch_rewrite_no_longer_demands_an_origin_run(monkeypatch: pytest.MonkeyPatch, draft) -> None:  # noqa: ANN001
    """对话派发路径同样不能再以“缺少原生成记录”拒绝。"""
    import inspect

    source = inspect.getsource(chat_dispatch._step_rewrite)

    assert "缺少可回溯的原生成记录" not in source
    assert "start_draft_rewrite" in source
