"""提示文案的标点不能重复：句号后不应再跟分号。

真实反馈：“另外这个提示一个句号一个分号什么原因”——
基础句自带句号（“……请查看生成记录后重试。”），拼接时又补了一个分号，
于是出现 `。；`。
"""

from __future__ import annotations

import pathlib

WORKER_SOURCE = pathlib.Path("app/worker.py").read_text(encoding="utf-8")


def _load_helpers():
    """只取两个纯函数，避免导入 app.worker 触发配置/日志初始化污染其它用例。"""
    namespace: dict[str, object] = {"GenerationError": type("GenerationError", (Exception,), {})}
    for name in ("_generation_failure_detail", "collection_memory_summary"):
        start = WORKER_SOURCE.index(f"def {name}(")
        end = WORKER_SOURCE.index("\n\n\n", start)
        exec(compile(WORKER_SOURCE[start:end], "worker_slice", "exec"), namespace)  # noqa: S102
    return namespace


def test_failure_detail_has_no_double_punctuation() -> None:
    helpers = _load_helpers()
    detail = helpers["_generation_failure_detail"](RuntimeError("boom"))

    assert detail == "内容生成执行失败，请查看生成记录后重试；未生成新的草稿。"
    assert "。；" not in detail


def test_failure_detail_keeps_preserved_draft_wording() -> None:
    helpers = _load_helpers()
    detail = helpers["_generation_failure_detail"](RuntimeError("boom"), preserved_draft=True)

    assert detail.endswith("原草稿与历史版本未被覆盖。")
    assert "。；" not in detail


def test_failure_detail_strips_generation_error_trailing_period() -> None:
    helpers = _load_helpers()
    error = helpers["GenerationError"]("供应商返回 429。")

    detail = helpers["_generation_failure_detail"](error)

    assert detail == "供应商返回 429；未生成新的草稿。"


def test_collection_memory_summary_joins_reasons_cleanly() -> None:
    helpers = _load_helpers()

    class _Run:
        def __init__(self) -> None:
            self.data = {"duplicates": 2, "skipped": 1}

        def get(self, key: str, default: int = 0) -> int:
            return self.data.get(key, default)

    class _Result:
        created = 0
        runs = [_Run()]

    summary = helpers["collection_memory_summary"](_Result(), "本轮采集完成。")

    assert summary.startswith("本轮采集完成；")
    assert "。；" not in summary
