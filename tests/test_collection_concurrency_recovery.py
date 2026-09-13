"""采集并发冲突与“运行记录被删”的容错。

真实故障（2026-09-13 23:24，用户报“内容生成执行失败”）：
- 两个采集任务并发运行（15:24:20 与 15:24:35 各一个），`save_source` 先查后插，
  双方都查到不存在 → 其中一个撞上 `uq_source_external_id` → IntegrityError → 任务失败；
- 失败收尾要写回**已被删除**的旧运行记录 → `RepositoryError` 把真正的 IntegrityError 掩盖。

注意：本文件**不在模块层导入 `app.worker`**——那会在测试收集阶段就初始化配置与日志，
污染其它依赖环境变量的用例（实测导致 24 项失败）。
"""

from __future__ import annotations

import inspect
import pathlib

from app.storage.repositories import ContentRepository

WORKER_SOURCE = pathlib.Path("app/worker.py").read_text(encoding="utf-8")


def test_source_insert_uses_savepoint_and_recovers_from_race() -> None:
    source = inspect.getsource(ContentRepository.save_source)

    assert "begin_nested()" in source            # SAVEPOINT：插入失败不影响外层事务
    assert "except IntegrityError" in source
    assert "source_insert_raced" in source       # 竞态被记录
    assert "self._apply_source_update(winner, item)" in source  # 复用已存在的行而不是抛错


def test_source_update_fields_are_shared_by_both_paths() -> None:
    """更新字段必须复用同一个方法，避免两条路径逐渐不一致。"""
    update = inspect.getsource(ContentRepository._apply_source_update)
    save = inspect.getsource(ContentRepository.save_source)

    assert "existing.title = item.title" in update
    assert "self._apply_source_update(existing, item)" in save


def test_agent_event_is_skipped_when_run_is_gone() -> None:
    source = inspect.getsource(ContentRepository.add_chat_agent_event)

    assert "chat_agent_event_skipped" in source
    assert "对话 Agent 运行记录不存在" not in source
    # 返回值变为可空：调用方不应假设一定有事件行
    assert "ChatAgentEventRow | None" in source


def test_failed_run_finish_tolerates_deleted_run() -> None:
    start = WORKER_SOURCE.index("def _finish_failed_run(")
    end = WORKER_SOURCE.index("def _generation_failure_detail(")
    source = WORKER_SOURCE[start:end]

    assert "failed_run_finish_skipped" in source
    # 运行记录已被删除时收尾只放弃写入，不再把 RepositoryError 抛出去掩盖真实失败原因
    assert "except RepositoryError" in source
    assert "session.rollback()" in source
