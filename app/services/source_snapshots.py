"""草稿生命周期内的私有来源正文快照。"""

from __future__ import annotations

import logging

from app.config import Settings
from app.domain.models import RawSourceItem, SourceKind
from app.services.attachments import AttachmentError, PrivateAttachmentStore
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.source_snapshots")


class SourceSnapshotError(RuntimeError):
    """快照不可用时阻止静默改用弱证据重生成。"""


class DraftSourceSnapshotStore:
    """README 仅在草稿审核期间保留于私有对象存储。"""

    def __init__(
        self, settings: Settings, repository: ContentRepository,
        object_store: PrivateAttachmentStore | None = None,
    ):
        self.repository = repository
        self.object_store = object_store or PrivateAttachmentStore(settings)

    def capture_github_readme(self, draft_id: str, item: RawSourceItem) -> bool:
        if (
            item.source_kind != SourceKind.GITHUB
            or item.metadata.get("readme_fetch_status") != "success"
            or item.metadata.get("content_origin") != "github_readme"
            or not item.content.strip()
        ):
            return False
        if self.repository.get_active_draft_source_snapshot(draft_id):
            return False
        encoded = item.content.encode("utf-8")
        try:
            object_key, content_hash = self.object_store.upload_internal_text_snapshot(encoded)
            try:
                self.repository.save_draft_source_snapshot(
                    draft_id=draft_id,
                    source_item_id=self.repository.get_draft(draft_id).source_item_id,
                    source_url=str(item.url),
                    content_origin="github_readme",
                    object_key=object_key,
                    sha256=content_hash,
                    content_length=len(item.content),
                )
            except Exception:
                self.object_store.delete(object_key)
                raise
        except AttachmentError as exc:
            raise SourceSnapshotError("README 私有快照保存失败") from exc
        logger.info("draft_source_snapshot_saved draft_id=%s origin=github_readme chars=%s", draft_id, len(item.content))
        return True

    def restore_github_readme(self, draft_id: str, item: RawSourceItem) -> RawSourceItem | None:
        snapshot = self.repository.get_active_draft_source_snapshot(draft_id)
        if snapshot is None:
            return None
        try:
            content = self.object_store.read(snapshot.object_key or "").decode("utf-8")
        except (AttachmentError, UnicodeDecodeError) as exc:
            raise SourceSnapshotError("README 私有快照不可读取，已停止重生成以保留原草稿") from exc
        metadata = {
            **item.metadata,
            "content_origin": "github_readme_snapshot",
            "readme_fetch_status": "cached",
            "readme_snapshot_id": snapshot.id,
        }
        logger.info("draft_source_snapshot_restored draft_id=%s chars=%s", draft_id, len(content))
        return item.model_copy(update={"content": content, "metadata": metadata})

    def replace_github_readme(self, draft_id: str, item: RawSourceItem) -> bool:
        """重新抓取后覆盖草稿的 README 快照（刷新来源专用）。

        `capture_github_readme` 见到已有快照会直接返回 False（防止采集链路重复上传）；
        刷新场景要的正是“用新抓到的正文换掉旧的”：先删旧对象与旧记录，再存新的——
        否则刷新后重生成仍会读到旧 README，看起来像“刷新没生效”。
        """
        previous = self.repository.get_active_draft_source_snapshot(draft_id)
        if previous is not None:
            try:
                self.object_store.delete(previous.object_key or "")
            except AttachmentError as exc:
                raise SourceSnapshotError("旧的 README 私有快照删除失败，已保留旧证据") from exc
            self.repository.delete_draft_source_snapshot(draft_id)
            logger.info("draft_source_snapshot_replaced draft_id=%s", draft_id)
        return self.capture_github_readme(draft_id, item)

    def delete_after_approval(self, draft_id: str) -> bool:
        snapshot = self.repository.get_active_draft_source_snapshot(draft_id)
        if snapshot is None:
            return False
        try:
            self.object_store.delete(snapshot.object_key or "")
        except AttachmentError as exc:
            raise SourceSnapshotError("README 私有快照删除失败，审核通过未提交") from exc
        self.repository.mark_draft_source_snapshot_deleted(draft_id)
        logger.info("draft_source_snapshot_deleted draft_id=%s", draft_id)
        return True
