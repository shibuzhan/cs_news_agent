"""来源快照的生命周期：首次读取→保存→**直到投递进公众号草稿箱**。

真实要求：“仍保持第一次读取保存 readme，直到该项目已投递到草稿箱”。
此前快照在**审核通过**时就被删除，导致通过之后的重写/重审/换图无法复用来源正文。
"""

from __future__ import annotations

import inspect

from app.api import routes
from app.services import auto_delivery


def test_approval_no_longer_deletes_the_snapshot() -> None:
    source = inspect.getsource(routes.review_draft)

    assert 'command.action == "discard"' in source
    assert 'command.action == "approve"' not in source


def test_auto_delivery_approval_keeps_the_snapshot() -> None:
    source = inspect.getsource(auto_delivery.auto_review_and_create_wechat_draft)

    # 审核通过分支里不得再删除快照
    assert "delete_after_approval" not in source
    assert "快照要保留到真正投递进公众号草稿箱" in source


def test_delivery_releases_the_snapshot() -> None:
    source = inspect.getsource(auto_delivery.retry_agent_selected_wechat_draft)

    assert source.count("release_source_snapshot_after_delivery(") >= 3  # 新建 / 覆盖 / 覆盖失败后重建
    release = inspect.getsource(auto_delivery.release_source_snapshot_after_delivery)
    assert "delete_after_approval" in release
    # 清理失败不能影响投递结果
    assert "except Exception" in release


def test_manual_delivery_route_releases_the_snapshot() -> None:
    source = inspect.getsource(routes.create_wechat_draft)

    assert "release_source_snapshot_after_delivery" in source
