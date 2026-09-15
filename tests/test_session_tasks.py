"""会话任务清单（跨消息的 todo）：登记、依赖、放行与收尾。

用户反馈（2026-09-14）：“目前 agent 是不是缺少 todolist 能力？收到什么消息就立刻去执行。”
这个文件锁住清单的核心语义：

- 同一件事**不会排两遍**（未完成的同标题任务直接复用）；
- 有前置的任务登记为“等待前置”，前置完成后自动变“待执行”，前置失败则“已跳过”；
- 多步指令会把每一步登记进清单并连好依赖；
- 后台任务结束时会把对应清单项收尾（否则清单永远停在“进行中”）。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services import session_tasks as tasks


class FakeRepository:
    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []
        self.counter = 0

    # --- 仓储接口（与 ContentRepository 同名同形，仅保留清单需要的部分） ---
    def _row(self, title: str, kind: str, status: str, depends_on: list[str], draft_id=None):
        self.counter += 1
        return SimpleNamespace(
            id=f"task-{self.counter}", session_id="session-1", position=tasks.KIND_ORDER.get(kind, 80),
            title=title, kind=kind, status=status, depends_on_json=list(depends_on),
            draft_id=draft_id, note="", created_at=None, updated_at=None,
        )

    def list_session_tasks(self, _session_id: str):
        return sorted(self.rows, key=lambda row: (row.position, row.id))

    def get_session_task(self, task_id: str):
        return next((row for row in self.rows if row.id == task_id), None)

    def create_session_task(self, session_id, title, kind, *, depends_on=None, draft_id=None, note="", status="pending"):  # noqa: ANN001
        row = self._row(title, kind, status, depends_on or [], draft_id)
        row.note = note
        self.rows.append(row)
        return row

    def update_session_task(self, task_id, *, status=None, note=None, depends_on=None):  # noqa: ANN001
        row = self.get_session_task(task_id)
        if row is None:
            return None
        if status is not None:
            row.status = status
        if note is not None:
            row.note = note
        if depends_on is not None:
            row.depends_on_json = list(depends_on)
        return row

    def list_session_tasks_by_dependency(self, task_id: str):
        return [row for row in self.rows if task_id in (row.depends_on_json or [])]


def test_task_without_dependency_is_ready_immediately() -> None:
    repository = FakeRepository()

    task = tasks.add_task(repository, "session-1", "自动审核", "review")

    assert task.status == tasks.STATUS_READY
    assert tasks.describe_task(task)["kind_label"] == "自动审核"


def test_running_task_uses_processing_label() -> None:
    assert tasks.status_label(tasks.STATUS_RUNNING) == "正在处理"


def test_dependent_task_waits_and_is_released_on_completion() -> None:
    repository = FakeRepository()
    review = tasks.add_task(repository, "session-1", "自动审核", "review")
    deliver = tasks.add_task(
        repository, "session-1", "投递公众号草稿", "deliver", depends_on=[review.id]
    )

    assert deliver.status == tasks.STATUS_PENDING, "有前置时先排队"

    tasks.complete_task(repository, review.id, "审核通过")

    assert repository.get_session_task(deliver.id).status == tasks.STATUS_READY, "前置完成后放行"


def test_dependent_task_is_skipped_when_the_blocker_fails() -> None:
    repository = FakeRepository()
    review = tasks.add_task(repository, "session-1", "自动审核", "review")
    deliver = tasks.add_task(
        repository, "session-1", "投递公众号草稿", "deliver", depends_on=[review.id]
    )

    tasks.fail_task(repository, review.id, "审核未通过")

    skipped = repository.get_session_task(deliver.id)
    assert skipped.status == tasks.STATUS_SKIPPED
    assert "前置任务未完成" in skipped.note


def test_the_same_open_task_is_not_queued_twice() -> None:
    repository = FakeRepository()

    first = tasks.add_task(repository, "session-1", "自动审核", "review")
    second = tasks.add_task(repository, "session-1", "自动审核", "review")

    assert first.id == second.id
    assert len(repository.rows) == 1


def test_summary_line_lists_only_open_tasks() -> None:
    repository = FakeRepository()
    done = tasks.add_task(repository, "session-1", "刷新来源", "refresh_source")
    tasks.complete_task(repository, done.id)
    tasks.add_task(repository, "session-1", "自动审核", "review")

    line = tasks.task_summary_line(repository.list_session_tasks("session-1"))

    assert "自动审核" in line
    assert "刷新来源" not in line


def test_plan_registers_each_step_with_dependencies() -> None:
    """多步指令（审核 → 投递）在清单里是两条带依赖的任务。"""
    from app.services import chat_dispatch

    source = inspect.getsource(chat_dispatch._record_plan_tasks)

    assert "add_task" in source
    assert "_TASK_DEPENDENCIES" in source
    assert chat_dispatch._TASK_DEPENDENCIES["deliver"] == ("review",)
    assert chat_dispatch._TASK_DEPENDENCIES["review"] == ("rewrite",)


def test_single_step_never_creates_a_todo_item() -> None:
    """单项命令的状态应留在对话运行记录，不能占用右侧多步清单。"""
    from app.services import chat_dispatch

    repository = FakeRepository()
    context = SimpleNamespace(repository=repository, session_id="session-1")

    recorded = chat_dispatch._record_plan_tasks(context, ("review",), deliver_after_review=False)

    assert recorded == {}
    assert repository.rows == []


def test_direct_commands_do_not_create_a_todo_item() -> None:
    """页面上的单项按钮不应绕过多步计划规则自行登记 Todo。"""
    from app.services import chat_dispatch

    source = inspect.getsource(chat_dispatch._dispatch_command)

    assert "_record_command_task" not in source


def test_worker_finalizes_tasks_after_background_jobs() -> None:
    """后台任务结束必须收尾清单，否则清单永远停在“进行中”。"""
    from app import worker

    for name in ("_report_auto_review_result", "_report_wechat_delivery_result", "process_draft_regeneration_job"):
        source = inspect.getsource(getattr(worker, name))
        assert "_advance_session_tasks" in source, f"{name} 没有收尾会话清单"


def test_worker_advance_marks_failure_and_skips_dependents() -> None:
    from app import worker

    repository = FakeRepository()
    review = tasks.add_task(repository, "session-1", "自动审核", "review")
    deliver = tasks.add_task(
        repository, "session-1", "投递公众号草稿", "deliver", depends_on=[review.id]
    )

    released = worker._advance_session_tasks(repository, "session-1", "review", note="未通过", done=False)

    assert repository.get_session_task(review.id).status == tasks.STATUS_FAILED
    assert [item.id for item in released] == [deliver.id]
    assert repository.get_session_task(deliver.id).status == tasks.STATUS_SKIPPED


def test_deep_agent_mounts_the_task_tools() -> None:
    """写了工具却没挂上等于没有：会话 Agent 必须能看到清单工具。"""
    import app.agents.content_deep_agent as deep

    source = inspect.getsource(deep.ContentDeepAgent._resolve_sync)

    assert "build_session_task_tools(session_id)" in source


def test_task_tools_expose_the_four_parts() -> None:
    from app.agent_tools.session_task_tools import build_session_task_tools

    names = [item.name for item in build_session_task_tools("session-1")]

    assert names == ["list_tasks", "add_task", "update_task", "clear_task"]
