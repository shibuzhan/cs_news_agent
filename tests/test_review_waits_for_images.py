"""“生成插图和封面图，然后审核”要按顺序执行，汇报不能自相矛盾。

真实反馈（2026-09-13 16:29，用户：“感觉这样不是很合理”）：
- 数据库时序：配图入队 16:29:03 → 封面绑定 16:29:04 → **审核 16:29:05** → 正文插图完成 16:29:13
  —— 审核比配图早 8 秒，审核看到的是“还没有正文插图”的状态；
- 汇报写着“本次采集未产出草稿…自动审核未被触发”，2 秒后同一轮又在审核；
- 实际是在**已有待审核草稿**上补了封面与插图（草稿 15:34 就存在）。
"""

from __future__ import annotations

import inspect
import json
import pathlib

from app.services import pending_reviews
from app.services.pending_reviews import (
    mark_pending_review,
    pending_review_drafts,
    pop_pending_review,
)
from app.services.task_narration import _SYSTEM
from app.storage.repositories import ContentRepository

WORKER_SOURCE = pathlib.Path("app/worker.py").read_text(encoding="utf-8")


class _Settings:
    collection_job_timeout_seconds = 60


class _Repository:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_app_setting(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)

    def set_app_setting(self, key: str, value: str, *, updated_by: str = "agent"):
        self.values[key] = value
        return type("Row", (), {"key": key, "value": value})()


def test_pending_review_round_trip() -> None:
    repository = _Repository()

    assert pending_review_drafts(repository, ["draft-1"]) == []
    mark_pending_review(repository, "draft-1", deliver=True, chat_run_id="run-1")
    assert pending_review_drafts(repository, ["draft-1", "draft-2"]) == ["draft-1"]

    popped = pop_pending_review(repository, "draft-1")
    assert popped == {"deliver": True, "chat_run_id": "run-1"}
    assert pop_pending_review(repository, "draft-1") is None       # 只取一次
    assert pending_review_drafts(repository, ["draft-1"]) == []


def test_review_waits_for_pending_images() -> None:
    """配图未完成时审核必须先排队，而不是立刻入队。"""
    source = inspect.getsource(__import__("app.agent_tools.draft_actions", fromlist=["x"]))

    assert "count_active_image_jobs" in source
    assert "queued_after_images" in source
    assert "配图完成后我会自动开始审核" in source
    # 仍然保留了配图完成后的正常审核路径
    assert "create_auto_review_run(draft.id, None, status=\"queued\")" in source


def test_finalizer_launches_queued_reviews_and_reports_real_facts() -> None:
    start = WORKER_SOURCE.index("async def process_collection_finalizer(")
    source = WORKER_SOURCE[start:]

    assert "launch_pending_reviews(" in source                      # 配图终态后真正发起审核
    assert "_draft_report_facts(repository, draft_ids)" in source
    assert '"drafts": drafts_facts' in source
    # 纯配图运行没有文字事件时，草稿来自图片任务本身
    assert "dict.fromkeys(task.draft_id for task in tasks)" in source


def test_draft_report_facts_include_source_and_ai_counts() -> None:
    source = inspect.getsource(__import__("app.worker", fromlist=["x"])._draft_report_facts)

    for field in (
        "illustrations_from_source",
        "illustrations_ai_generated",
        "review_status",
        "review_active",
    ):
        assert field in source


def test_narration_rules_forbid_contradictory_reporting() -> None:
    assert "不要用“未产出草稿”“没有草稿”掩盖" in _SYSTEM
    assert "才能说“未触发审核”" in _SYSTEM
    assert "illustrations_from_source" in _SYSTEM


def test_pending_review_repository_helper_exists() -> None:
    assert hasattr(ContentRepository, "count_active_image_jobs")
    assert "queued" in inspect.getsource(ContentRepository.count_active_image_jobs)


def test_pending_review_helper_is_json_serialisable() -> None:
    repository = _Repository()
    mark_pending_review(repository, "d", deliver=False, chat_run_id=None)
    assert json.loads(repository.values[pending_reviews.pending_key("d")]) == {
        "deliver": False,
        "chat_run_id": "",
    }
