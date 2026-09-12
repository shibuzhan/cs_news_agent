"""反复覆盖投递不应重复上传封面永久素材。

真实问题：素材库里同一张封面出现 4 份——因为每次覆盖都把封面重新上传为永久素材，
而永久素材**计入素材库配额**（图片类通常上限 5000 张）。
"""

from __future__ import annotations

import inspect

from app.services import auto_delivery
from app.storage.repositories import ContentRepository


def test_selection_reuses_cover_media_id_when_cover_unchanged() -> None:
    source = inspect.getsource(ContentRepository.save_wechat_asset_selection)

    assert "same_cover = row.cover_asset_id == cover_asset_id" in source
    assert "reused_cover_media_id = row.cover_media_id if same_cover else None" in source
    assert "same_inline = list(row.inline_asset_ids_json or []) == list(inline_asset_ids)" in source


def test_stale_marking_keeps_previous_selection_for_reuse() -> None:
    source = inspect.getsource(ContentRepository.mark_wechat_publication_stale)

    # 不再清空旧选择：保留它才能判断“封面是否仍是同一张”并复用 media_id。
    assert "row.cover_asset_id = None" not in source
    assert "row.inline_image_urls_json = []" not in source
    assert 'row.state = "delivery_stale"' in source


def test_stale_job_still_reselects() -> None:
    """标记过期必须重新选择，否则“重新选择配图”会变成空操作。"""
    source = inspect.getsource(auto_delivery.ensure_agent_selected_wechat_assets)

    assert '"superseded", "delivery_stale"' in source


def test_prepare_reuses_uploaded_assets() -> None:
    source = inspect.getsource(auto_delivery.prepare_agent_selected_wechat_assets)

    assert "needs_cover = not job.cover_media_id" in source
    assert "needs_inline = bool(job.inline_asset_ids_json) and not job.inline_image_urls_json" in source
    assert "job.cover_media_id or await client.upload_cover(" in source
    assert "wechat_assets_reused" in source
