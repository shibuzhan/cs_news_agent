"""草稿归属与“点了按钮却被拒”的回归测试。

真实故障（用户截图）：草稿 `b3857275…` 明明是会话 `be2948a0…` 的当前文章（配图、重写都在这个
会话里做过），但界面命令“重写文案｜draft=…”仍被判“该草稿不属于本会话”——因为归属只查了
“本会话的 collect_news 运行事件里有没有 draft_ids”，而更早生成的草稿根本没有这个字段。
另外报告一度让用户“回复‘在当前会话重新打开该草稿’”，而系统并没有这种指令。
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from app.agent_tools import draft_actions
from app.agent_tools.draft_actions import _draft_belongs_to_session
from app.services import chat_dispatch
from app.services.chat_dispatch import ChatDispatchContext, _dispatch_command


class FakeRepository:
    """只实现归属判定与“设为本会话当前文章”两条路径。"""

    def __init__(self, *, memory_draft: str | None = None, referenced: bool = False) -> None:
        self.memory_draft = memory_draft
        self.referenced = referenced
        self.adopted: list[str] = []
        self.events: list[tuple] = []

    def session_references_draft(self, _session_id: str, draft_id: str) -> bool:
        return self.referenced or self.memory_draft == draft_id

    def get_draft(self, draft_id: str):
        return SimpleNamespace(
            id=draft_id, title_options_json=["openai/plugins：示例"], status="pending_review"
        )

    def update_chat_session_memory(self, _session_id: str, *, active_draft_id: str) -> None:
        self.adopted.append(active_draft_id)
        self.memory_draft = active_draft_id

    # --- 会话任务清单（命令路径也会登记一条待办） ---
    def get_chat_session_memory(self, _session_id: str):
        return SimpleNamespace(active_draft_id=self.memory_draft)

    tasks: list[SimpleNamespace]

    def list_session_tasks(self, _session_id: str):
        return getattr(self, "tasks", [])

    def get_session_task(self, task_id: str):
        return next((row for row in getattr(self, "tasks", []) if row.id == task_id), None)

    def create_session_task(self, session_id, title, kind, *, depends_on=None, draft_id=None, note="", status="pending"):  # noqa: ANN001
        rows = getattr(self, "tasks", None)
        if rows is None:
            rows = self.tasks = []
        row = SimpleNamespace(
            id=f"task-{len(rows) + 1}", session_id=session_id, position=0, title=title, kind=kind,
            status=status, depends_on_json=list(depends_on or []), draft_id=draft_id, note=note,
        )
        rows.append(row)
        return row

    def update_session_task(self, task_id, *, status=None, note=None, depends_on=None):  # noqa: ANN001
        row = self.get_session_task(task_id)
        if row is None:
            return None
        if status is not None:
            row.status = status
        if note is not None:
            row.note = note
        return row

    def list_session_tasks_by_dependency(self, task_id: str):
        return [row for row in getattr(self, "tasks", []) if task_id in (row.depends_on_json or [])]

    def add_chat_agent_event(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.events.append((args, kwargs))


def test_draft_belongs_when_it_is_the_session_current_article() -> None:
    repository = FakeRepository(memory_draft="draft-1")

    assert _draft_belongs_to_session(repository, "session-1", "draft-1") is True
    assert _draft_belongs_to_session(repository, "session-1", "draft-2") is False


def test_draft_belongs_when_a_session_run_referenced_it() -> None:
    assert _draft_belongs_to_session(FakeRepository(referenced=True), "session-1", "draft-9") is True


def test_button_command_adopts_an_explicitly_clicked_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """界面命令带着用户点出来的 draft_id：本会话没用过也要先接管，而不是拒绝。"""
    repository = FakeRepository()
    captured: dict[str, object] = {}

    class FakeTool:
        name = "rewrite_draft"

        async def ainvoke(self, arguments):  # noqa: ANN001, ANN202
            captured["arguments"] = arguments
            return {"status": "done", "message": "已重写《openai/plugins：示例》。"}

    async def fake_compose(*_args, **_kwargs) -> str:
        return "已重写这篇。"

    monkeypatch.setattr(chat_dispatch, "build_draft_action_tools", lambda *_a, **_k: [FakeTool()])
    monkeypatch.setattr(chat_dispatch, "_compose_reply", fake_compose)

    ctx = ChatDispatchContext(
        settings=SimpleNamespace(),
        repository=repository,
        session=SimpleNamespace(commit=lambda: None),
        session_id="session-1",
        run_id="run-1",
        content="重写文案｜draft=draft-77",
    )
    command = SimpleNamespace(
        name="rewrite_draft", label="重写文案", draft_id="draft-77", purpose="cover", placement=0
    )

    outcome = asyncio.run(_dispatch_command(ctx, command))

    assert repository.adopted == ["draft-77"], "显式点选的草稿必须先被本会话接管"
    assert captured["arguments"] == {"draft_id": "draft-77"}
    assert repository.events[0][0][1] == "已把这篇设为本会话当前文章"
    assert outcome is not None and outcome.reply == "已重写这篇。"


def test_missing_ownership_message_points_at_a_real_next_step() -> None:
    """拒绝话术必须给出系统真实支持的下一步（此前模型编了不存在的“回复某某”）。"""
    source = inspect.getsource(draft_actions)

    assert "先让我把它设为本会话当前文章" in source


def test_narration_forbids_inventing_next_steps() -> None:
    from app.services import task_narration

    system = task_narration._SYSTEM

    assert "只允许建议系统真实支持的下一步" in system
    assert "不要发明关键词或命令" in system
    # 用户反馈：返回信息应该是稿件标题，不是内部 id。
    assert "一律用稿件标题指代文章" in system
    assert "不要把 draft_id" in system
