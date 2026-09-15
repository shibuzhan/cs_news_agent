"""会话任务清单：跨消息的“要做什么、做到哪一步、还等谁”。

为什么需要它（真实反馈 2026-09-14）：“目前 agent 是不是缺少 todolist 能力？收到什么消息就立刻去执行。”
此前的状态是：回执里的“共 2 步”只是一句文案（不落库、无状态），DeepAgent 每条消息只解析一个意图、
不检查前置条件，于是“审核如果通过就投递”被当成两条并列任务，第一条一失败整条就断。

分工：
- `add_task` / `complete_task` / `skip_task` 由**动作入口**调用（对话派发、Agent 工具、worker 收尾）；
- `advance_ready_tasks` 在任务完成时把满足依赖的后继任务置为 `ready`（不自动执行：
  真正发起动作仍由对话/工具完成，避免后台悄悄替用户做决定）；
- `describe_task` / `task_summary_line` 给回执与前端用。
"""

from __future__ import annotations

import logging
from typing import Any

from app.storage.repositories import ContentRepository

logger = logging.getLogger("news_agent.session_tasks")

# 状态机：pending（还等前置）→ ready（可以做）→ running（在做）→ completed / failed / skipped
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
OPEN_STATUSES = (STATUS_PENDING, STATUS_READY, STATUS_RUNNING)
BLOCKING_STATUSES = (STATUS_FAILED, STATUS_SKIPPED)

KIND_LABELS = {
    "collect": "采集资讯",
    "rewrite": "重写文案",
    "refresh_source": "刷新来源",
    "review": "自动审核",
    "deliver": "投递公众号草稿",
    "image": "生成配图",
    "chat": "会话跟进",
    "other": "其它",
}

# 动作代号 → 清单顺序（数值越小越靠前）；同号按创建顺序。
KIND_ORDER = {
    "collect": 10,
    "refresh_source": 20,
    "rewrite": 30,
    "image": 40,
    "review": 50,
    "deliver": 60,
    "chat": 70,
    "other": 80,
}

STATUS_LABELS = {
    STATUS_PENDING: "等待前置",
    STATUS_READY: "待执行",
    STATUS_RUNNING: "正在处理",
    STATUS_COMPLETED: "已完成",
    STATUS_FAILED: "失败",
    STATUS_SKIPPED: "已跳过",
}


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, KIND_LABELS["other"])


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def describe_task(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "title": task.title,
        "kind": task.kind,
        "kind_label": kind_label(task.kind),
        "status": task.status,
        "status_label": status_label(task.status),
        "depends_on": list(task.depends_on_json or []),
        "draft_id": task.draft_id,
        "note": task.note or "",
    }


def add_task(
    repository: ContentRepository,
    session_id: str,
    title: str,
    kind: str = "other",
    *,
    depends_on: list[str] | None = None,
    draft_id: str | None = None,
    note: str = "",
    status: str | None = None,
) -> Any:
    """登记一条任务；**同标题未完成的重复登记直接复用**（避免同一件事排两遍）。"""
    clean_title = " ".join(str(title).split())[:200]
    if not clean_title:
        raise ValueError("任务标题不能为空")
    resolved_kind = kind if kind in KIND_LABELS else "other"
    for existing in repository.list_session_tasks(session_id):
        if existing.title == clean_title and existing.status in OPEN_STATUSES:
            logger.info("session_task_reused session_id=%s task_id=%s", session_id, existing.id)
            return existing
    return repository.create_session_task(
        session_id,
        clean_title,
        resolved_kind,
        depends_on=depends_on or [],
        draft_id=draft_id,
        note=note,
        status=status or (STATUS_PENDING if depends_on else STATUS_READY),
    )


def complete_task(
    repository: ContentRepository, task_id: str, note: str = ""
) -> tuple[Any | None, list[Any]]:
    """标记完成，并放行（或跳过）依赖它的后继；返回（本任务, 被放行的后继）。

    放行只做一次：重复调用 `release_dependents` 会因为没有候选而返回空，
    于是“这条任务放行了谁”就在第二次调用里丢掉（真实 bug：清单收了尾却报不出后继）。
    """
    task = repository.update_session_task(task_id, status=STATUS_COMPLETED, note=note or None)
    released = release_dependents(repository, task_id) if task is not None else []
    return task, released


def fail_task(
    repository: ContentRepository, task_id: str, note: str = ""
) -> tuple[Any | None, list[Any]]:
    task = repository.update_session_task(task_id, status=STATUS_FAILED, note=note or None)
    released = release_dependents(repository, task_id) if task is not None else []
    return task, released


def release_dependents(repository: ContentRepository, task_id: str) -> list[Any]:
    """前置任务结束后处理后继：能做的置 ready，前置失败的跟着跳过。"""
    changed: list[Any] = []
    for candidate in repository.list_session_tasks_by_dependency(task_id):
        if candidate.status not in (STATUS_PENDING,):
            continue
        blockers = [
            repository.get_session_task(dep)
            for dep in (candidate.depends_on_json or [])
        ]
        blockers = [item for item in blockers if item is not None]
        if any(item.status in BLOCKING_STATUSES for item in blockers):
            updated = repository.update_session_task(
                candidate.id, status=STATUS_SKIPPED, note="前置任务未完成，已跳过"
            )
        elif all(item.status == STATUS_COMPLETED for item in blockers):
            updated = repository.update_session_task(candidate.id, status=STATUS_READY)
        else:
            continue
        if updated is not None:
            changed.append(updated)
            logger.info(
                "session_task_released task_id=%s status=%s", updated.id, updated.status
            )
    return changed


def open_tasks(repository: ContentRepository, session_id: str) -> list[Any]:
    return [
        task for task in repository.list_session_tasks(session_id)
        if task.status in OPEN_STATUSES
    ]


def ready_task_for_kind(
    repository: ContentRepository, session_id: str, kind: str, draft_id: str | None = None,
) -> Any | None:
    """找到“这件事正在清单里等着做”的那一条（用于动作入口复用而不是新建）。"""
    for task in open_tasks(repository, session_id):
        if task.kind != kind:
            continue
        if draft_id and task.draft_id and task.draft_id != draft_id:
            continue
        return task
    return None


def task_summary_line(tasks: list[Any]) -> str:
    """一行清单摘要（回执/卡片标题用）：只列未完成的，且最多 3 条。"""
    open_items = [task for task in tasks if task.status in OPEN_STATUSES]
    if not open_items:
        return ""
    labels = [f"{kind_label(task.kind)}（{status_label(task.status)}）" for task in open_items[:3]]
    extra = f" 等 {len(open_items)} 项" if len(open_items) > 3 else ""
    return "清单：" + " → ".join(labels) + extra
