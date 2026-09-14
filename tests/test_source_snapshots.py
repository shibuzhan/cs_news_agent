from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

from app.domain.models import RawSourceItem, SourceKind
from app.services.source_snapshots import DraftSourceSnapshotStore


@dataclass
class FakeSnapshot:
    id: str = "snapshot-1"
    draft_id: str = "draft-1"
    source_item_id: str = "source-1"
    source_url: str = "https://github.com/example/repo"
    content_origin: str = "github_readme"
    object_key: str | None = "source-snapshots/snapshot-1.md"
    sha256: str = "a" * 64
    content_length: int = 0
    content_deleted_at: object | None = None


class FakeRepository:
    def __init__(self) -> None:
        self.snapshot: FakeSnapshot | None = None

    def get_active_draft_source_snapshot(self, draft_id: str):
        if self.snapshot and self.snapshot.draft_id == draft_id and self.snapshot.object_key:
            return self.snapshot
        return None

    def get_draft(self, draft_id: str):
        return SimpleNamespace(source_item_id="source-1")

    def save_draft_source_snapshot(self, **kwargs):
        if self.snapshot is None:
            self.snapshot = FakeSnapshot(id="snapshot-1", **kwargs)
        return self.snapshot

    def mark_draft_source_snapshot_deleted(self, draft_id: str):
        assert self.snapshot is not None
        self.snapshot.object_key = None
        self.snapshot.content_deleted_at = datetime.now(UTC)
        return self.snapshot

    def delete_draft_source_snapshot(self, draft_id: str) -> int:
        """与真实仓储一致：硬删行，下一次保存才会写入新快照。"""
        if self.snapshot is None or self.snapshot.draft_id != draft_id:
            return 0
        self.snapshot = None
        return 1


class FakeObjectStore:
    def __init__(self) -> None:
        self.content: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def upload_internal_text_snapshot(self, content: bytes) -> tuple[str, str]:
        key = "source-snapshots/snapshot-1.md"
        self.content[key] = content
        return key, "a" * 64

    def read(self, object_key: str) -> bytes:
        return self.content[object_key]

    def delete(self, object_key: str) -> None:
        self.deleted.append(object_key)
        self.content.pop(object_key, None)


def github_readme() -> RawSourceItem:
    return RawSourceItem(
        source_kind=SourceKind.GITHUB,
        external_id="example/repo",
        title="example/repo",
        url="https://github.com/example/repo",
        published_at=datetime.now(UTC),
        summary="README 摘要",
        content="# Example\n\nA durable README snapshot.",
        source_name="GitHub",
        metadata={"readme_fetch_status": "success", "content_origin": "github_readme"},
    )


def test_github_readme_snapshot_is_reused_until_approval() -> None:
    repository = FakeRepository()
    object_store = FakeObjectStore()
    snapshots = DraftSourceSnapshotStore(SimpleNamespace(), repository, object_store)
    source = github_readme()

    assert snapshots.capture_github_readme("draft-1", source) is True
    assert snapshots.capture_github_readme("draft-1", source) is False

    restored = snapshots.restore_github_readme(
        "draft-1", source.model_copy(update={"content": "stale content"})
    )
    assert restored is not None
    assert restored.content == source.content
    assert restored.metadata["content_origin"] == "github_readme_snapshot"
    assert restored.metadata["readme_fetch_status"] == "cached"

    assert snapshots.delete_after_approval("draft-1") is True
    assert object_store.deleted == ["source-snapshots/snapshot-1.md"]
    assert repository.snapshot is not None
    assert repository.snapshot.object_key is None
    assert snapshots.restore_github_readme("draft-1", source) is None


def test_non_readme_source_is_not_snapshotted() -> None:
    repository = FakeRepository()
    snapshots = DraftSourceSnapshotStore(SimpleNamespace(), repository, FakeObjectStore())
    source = github_readme().model_copy(update={"metadata": {"readme_fetch_status": "failed"}})

    assert snapshots.capture_github_readme("draft-1", source) is False
    assert repository.snapshot is None


def test_replace_readme_snapshot_overwrites_the_old_one() -> None:
    """刷新来源必须真的换掉旧快照：否则重写仍读到旧 README，看起来像“刷新没生效”。"""
    repository = FakeRepository()
    object_store = FakeObjectStore()
    snapshots = DraftSourceSnapshotStore(SimpleNamespace(), repository, object_store)
    source = github_readme()
    assert snapshots.capture_github_readme("draft-1", source) is True

    refreshed = source.model_copy(update={"content": "# Example\n\nBrand new README body."})
    assert snapshots.replace_github_readme("draft-1", refreshed) is True

    restored = snapshots.restore_github_readme("draft-1", source)
    assert restored is not None
    assert "Brand new README body." in restored.content, "刷新后必须读到新正文"
    assert object_store.deleted == ["source-snapshots/snapshot-1.md"], "旧快照要从对象存储里删掉"


def test_replace_readme_snapshot_works_without_a_previous_one() -> None:
    repository = FakeRepository()
    snapshots = DraftSourceSnapshotStore(SimpleNamespace(), repository, FakeObjectStore())

    assert snapshots.replace_github_readme("draft-1", github_readme()) is True
    assert repository.snapshot is not None
