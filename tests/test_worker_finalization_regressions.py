"""重写/重新生成必须真的落库，收尾任务不能再因为缺 import 崩掉。

真实故障（用户实测，两个叠加）：
1. `process_collection_finalizer` 里用了 `ConversationIntent` 却没 import → 每次收尾都
   `NameError` → 运行永远停在“处理中”（图片/重写流程的收尾全废）。
2. `_regenerate_draft_in_thread` 没有提交：`regenerate_draft` 只 flush，而 `with SessionLocal()`
   退出会回滚 → 运行报告“已更新原草稿至版本 2”，数据库里却还是版本 1。
"""

from __future__ import annotations

import inspect

import pytest


def test_worker_imports_conversation_intent() -> None:
    from app import worker
    from app.domain.models import ConversationIntent

    assert ConversationIntent is worker.ConversationIntent
    finalizer = inspect.getsource(worker.process_collection_finalizer)
    assert "ConversationIntent.GENERATE_DRAFT_IMAGE" in finalizer


def test_regeneration_thread_commits_the_new_version() -> None:
    from app import worker

    source = inspect.getsource(worker._regenerate_draft_in_thread)

    assert "session.commit()" in source, "重写结果必须提交，否则 with SessionLocal() 退出时被回滚"
    assert "regenerate_draft" in source


def test_finalizer_source_has_no_undefined_names() -> None:
    """收尾函数里引用到的模块级名字必须都能解析（此前 ConversationIntent 漏 import）。"""
    from app import worker

    source = inspect.getsource(worker.process_collection_finalizer)
    compile(source, "<finalizer>", "exec")  # 语法层面先保证可用
    for name in ("ConversationIntent", "ConversationRunStatus", "compose_task_reply"):
        assert hasattr(worker, name), f"worker 缺少 {name}"


@pytest.mark.parametrize("name", ["process_collection_finalizer", "process_draft_regeneration_job", "_regenerate_draft_in_thread"])
def test_key_worker_functions_are_importable(name: str) -> None:
    from app import worker

    assert callable(getattr(worker, name))
