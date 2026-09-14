"""刷新来源（② 拆分）在存储层必须真的换掉证据，而不是“看起来刷新了”。

三处容易写错的地方，都用真实源码断言钉住：
1. `record_source_refresh` 只涨版本、正文原样保留，并让旧投递失效；
2. `delete_draft_source_snapshot` 必须**硬删行**，否则 `save_draft_source_snapshot` 会复用旧行，
   新快照存不进去（刷新后重写仍读到旧 README）；
3. `_apply_source_update` 要保留 `content_origin` 标记，否则刷新后数据库又显示“正文来自 Trending 简介”。
"""

from __future__ import annotations

import inspect

from app.services.source_snapshots import DraftSourceSnapshotStore
from app.storage.repositories import ContentRepository


def test_source_refresh_records_a_version_without_touching_the_body() -> None:
    source = inspect.getsource(ContentRepository.record_source_refresh)

    assert "row.version += 1" in source
    assert '"kind": "source_refresh"' in source
    assert "body=row.body" in source, "这一版沿用当前正文，回退时不会丢内容"
    assert "row.summary_cn" in source and "tags_json=list(row.tags_json or [])" in source
    assert "已发布或已废弃草稿不能刷新来源" in source


def test_source_refresh_invalidates_a_stale_delivery() -> None:
    """来源变了，之前投递依据的证据就过时：必须让下次投递覆盖远端草稿。"""
    source = inspect.getsource(ContentRepository.record_source_refresh)

    assert "invalidate_unfinished_wechat_publication_for_regeneration" in source
    assert "stale_delivery" in source


def test_snapshot_delete_is_a_hard_delete_so_a_new_one_can_be_saved() -> None:
    repository_source = inspect.getsource(ContentRepository.delete_draft_source_snapshot)
    save_source = inspect.getsource(ContentRepository.save_draft_source_snapshot)

    assert "self.session.delete(row)" in repository_source, "只清空 object_key 会让新快照存不上"
    assert repository_source.count("self.session.delete(row)") == 1
    # 旧行存在时 save 直接返回旧行：这正是必须先硬删的原因。
    assert "if existing:" in save_source and "return existing" in save_source


def test_replace_readme_drops_the_old_object_before_saving() -> None:
    source = inspect.getsource(DraftSourceSnapshotStore.replace_github_readme)

    assert "self.object_store.delete(previous.object_key or \"\")" in source
    assert "delete_draft_source_snapshot" in source
    assert "return self.capture_github_readme(draft_id, item)" in source
    # 删旧失败必须停手：不能把草稿的证据链删掉却没有新证据。
    assert "SourceSnapshotError" in source


def test_source_update_keeps_the_readme_origin_marker() -> None:
    source = inspect.getsource(ContentRepository._apply_source_update)

    assert "content_origin" in source
    assert "existing.metadata_json = metadata" in source
