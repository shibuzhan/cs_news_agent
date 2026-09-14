"""慢操作必须走后台任务：对话请求内只允许入队。

真实故障：把“下载图片 + 上传公众号图片 + 创建/覆盖草稿”放在对话工具里同步执行，
必然超过对话请求时限（用户看到“对话模型请求超时”）。
"""

from __future__ import annotations

import inspect

from app import worker
from app.agent_tools import draft_actions


def test_publish_tool_enqueues_instead_of_calling_wechat_inline() -> None:
    source = inspect.getsource(draft_actions)

    assert "enqueue_wechat_delivery_job" in source
    # 工具内不得直接调用投递实现（那会在请求内上传图片）。
    assert "retry_agent_selected_wechat_draft" not in source
    assert '"status": "started"' in source


def test_delivery_job_is_registered_on_the_worker() -> None:
    names = {item.name for item in worker.WorkerSettings.functions}

    assert "process_wechat_delivery_job" in names


def test_delivery_job_reports_through_the_model() -> None:
    source = inspect.getsource(worker.process_wechat_delivery_job)

    assert "retry_agent_selected_wechat_draft" in source
    assert "_report_wechat_delivery_result" in source
    # 汇报统一走 _compose_report：它内部会用流式版本（失败自动回退非流式）。
    report_source = inspect.getsource(worker._report_wechat_delivery_result)
    assert "_compose_report" in report_source
    assert "compose_task_reply_streaming" in inspect.getsource(worker._compose_report)


def test_source_image_download_budget_is_short() -> None:
    """来源图片下载在对话请求内执行，必须使用很短的超时预算。"""
    from app.agent_tools import source_media_tools

    source = inspect.getsource(source_media_tools)

    assert "read=15" in source
    assert "read=60" not in source
