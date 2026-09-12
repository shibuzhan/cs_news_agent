"""投递说明文本不得被当成“投递失败”上报。

真实反馈：通知中心反复出现“公众号投递失败：视觉选图：重新识别官方图后选择投递素材。”
——那是标记 `delivery_stale` 时写入的**说明**，被 `sync_failure_notifications()`
按 `error_message is not null` 一律当成失败，每次拉取通知都会重新报一次。
"""

from __future__ import annotations

import inspect

from app.storage.repositories import ContentRepository


def test_only_real_failure_states_raise_delivery_notifications() -> None:
    source = inspect.getsource(ContentRepository.sync_failure_notifications)

    assert 'state.in_(("draft_failed", "delivery_failed"))' in source
    # 不能再按“有 error_message 就是失败”判断：说明性文本也写在这个字段里。
    assert "WechatPublicationJobRow.error_message.is_not(None)" not in source
