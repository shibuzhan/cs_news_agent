"""会话任务清单 Tool：让 Agent 能“先列清单，再照着做”，而不是收到消息就立刻执行。

真实反馈（2026-09-14）：“目前 agent 是不是缺少 todolist 能力？收到什么消息就立刻去执行。”
这里提供四个零件：`add_task` / `list_tasks` / `update_task` / `clear_task`，
任务落库在 `session_tasks`（会话级、跨消息存活、可带依赖），
回执与生成记录会渲染同一份清单。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from app.services import session_tasks as tasks
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository

logger = logging.getLogger("news_agent.session_task_tools")

MAX_TITLE_CHARS = 200
MAX_NOTE_CHARS = 500


def _impl_list_tasks(session_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        repository = ContentRepository(session)
        rows = repository.list_session_tasks(session_id)
        items = [tasks.describe_task(task) for task in rows]
        summary = tasks.task_summary_line(rows)
    open_count = len([item for item in items if item["status"] in {"pending", "ready", "running"}])
    return {
        "status": "ok",
        "tasks": items,
        "open_count": open_count,
        "message": (
            f"当前清单共 {len(items)} 条，其中未完成 {open_count} 条。" + (summary or "没有待办。")
        )
        if items
        else "当前清单是空的：还没有登记任何待办任务。",
    }


def _impl_add_task(
    session_id: str,
    title: str,
    kind: str = "other",
    depends_on: list[str] | None = None,
    draft_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    with SessionLocal() as session:
        repository = ContentRepository(session)
        task = tasks.add_task(
            repository, session_id, str(title)[:MAX_TITLE_CHARS], kind,
            depends_on=[str(item) for item in (depends_on or []) if str(item).strip()],
            draft_id=draft_id or None,
            note=str(note)[:MAX_NOTE_CHARS],
        )
        items = [tasks.describe_task(row) for row in repository.list_session_tasks(session_id)]
        session.commit()
    logger.info("agent_tool_add_task session_id=%s task_id=%s kind=%s", session_id, task.id, task.kind)
    return {
        "status": "done",
        "task": tasks.describe_task(task),
        "tasks": items,
        "message": (
            f"已把「{task.title}」记进任务清单（{tasks.kind_label(task.kind)}，{tasks.status_label(task.status)}）。"
            + ("它要等前置任务完成后才能执行。" if task.status == tasks.STATUS_PENDING else "")
        ),
    }


def _impl_update_task(
    session_id: str, task_id: str, status: str = "", note: str = "",
) -> dict[str, Any]:
    allowed = {
        tasks.STATUS_PENDING, tasks.STATUS_READY, tasks.STATUS_RUNNING,
        tasks.STATUS_COMPLETED, tasks.STATUS_FAILED, tasks.STATUS_SKIPPED,
    }
    if status and status not in allowed:
        return {"status": "rejected", "message": f"不支持的状态：{status}（可用：{'、'.join(sorted(allowed))}）"}
    with SessionLocal() as session:
        repository = ContentRepository(session)
        row = repository.get_session_task(task_id)
        if row is None or row.session_id != session_id:
            return {"status": "rejected", "message": "找不到这条任务（或不属于本会话）。"}
        if status == tasks.STATUS_COMPLETED:
            updated, released = tasks.complete_task(repository, task_id, note)
        elif status == tasks.STATUS_FAILED:
            updated, released = tasks.fail_task(repository, task_id, note)
        else:
            updated = repository.update_session_task(
                task_id, status=status or None, note=(note[:MAX_NOTE_CHARS] or None)
            )
            released = tasks.release_dependents(repository, task_id) if updated is not None else []
        items = [tasks.describe_task(item) for item in repository.list_session_tasks(session_id)]
        session.commit()
    if updated is None:
        return {"status": "rejected", "message": "任务更新失败。"}
    logger.info("agent_tool_update_task task_id=%s status=%s released=%s", task_id, updated.status, len(released))
    released_labels = [tasks.kind_label(item.kind) for item in released]
    return {
        "status": "done",
        "task": tasks.describe_task(updated),
        "released": [tasks.describe_task(item) for item in released],
        "tasks": items,
        "message": (
            f"「{updated.title}」已标记为{tasks.status_label(updated.status)}。"
            + (f"现在轮到：{'、'.join(released_labels)}。" if released_labels else "")
        ),
    }


def _impl_clear_task(session_id: str, task_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        repository = ContentRepository(session)
        row = repository.get_session_task(task_id)
        if row is None or row.session_id != session_id:
            return {"status": "rejected", "message": "找不到这条任务（或不属于本会话）。"}
        title = row.title
        repository.session.delete(row)
        for candidate in repository.list_session_tasks(session_id):
            deps = [dep for dep in (candidate.depends_on_json or []) if dep != task_id]
            if deps != (candidate.depends_on_json or []):
                repository.update_session_task(candidate.id, depends_on=deps)
        tasks.release_dependents(repository, task_id)
        items = [tasks.describe_task(item) for item in repository.list_session_tasks(session_id)]
        session.commit()
    return {
        "status": "done",
        "tasks": items,
        "message": f"已从清单里移除「{title}」。",
    }


def build_session_task_tools(session_id: str):
    """构造只作用于本会话的任务清单 Tool。"""

    @tool("list_tasks")
    def list_tasks() -> dict[str, Any]:
        """查看本会话的任务清单：每条的状态（等待前置/待执行/进行中/已完成/失败/跳过）与依赖。

        长任务（重写 9–12 分钟、审核几分钟）期间先看这里，就知道“还差什么、在等谁”，
        不会因为看不到状态而重复发起同一个任务。
        """
        return _impl_list_tasks(session_id)

    @tool("add_task")
    def add_task(
        title: str,
        kind: str = "other",
        depends_on: list[str] | None = None,
        draft_id: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """把一件事记进任务清单（跨消息保留）。

        `kind` 用动作代号：collect / refresh_source / rewrite / image / review / deliver / chat / other；
        `depends_on` 填前置任务的 task_id（例如“投递”依赖“审核通过”），
        前置没完成时这条会显示“等待前置”，前置失败时自动标记“已跳过”。
        用户一次提出多件事（“先审核，通过后投递”）时，先各记一条并连好依赖，再逐条执行。
        """
        return _impl_add_task(
            session_id, title, kind=kind, depends_on=depends_on, draft_id=draft_id, note=note
        )

    @tool("update_task")
    def update_task(task_id: str, status: str = "", note: str = "") -> dict[str, Any]:
        """更新任务状态：标记进行中/已完成/失败/跳过；完成或失败会自动放行（或跳过）它的后继任务。"""
        return _impl_update_task(session_id, task_id, status=status, note=note)

    @tool("clear_task")
    def clear_task(task_id: str) -> dict[str, Any]:
        """从清单里移除一条任务（用户说“这件事不做了”时用）。"""
        return _impl_clear_task(session_id, task_id)

    return [list_tasks, add_task, update_task, clear_task]
