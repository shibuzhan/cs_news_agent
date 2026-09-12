from types import SimpleNamespace

import pytest

from app.domain.models import ManualPublicationCommand, ReviewStatus
from app.storage.repositories import ContentRepository, InvalidReviewTransition
from app.storage.tables import ProjectIntroductionRow, PublicationRecordRow, WechatPublicationJobRow


def test_manual_github_publication_marks_introduction_only_after_link_is_recorded():
    class FakeSession:
        def __init__(self) -> None:
            self.added = []

        def scalar(self, _statement):
            return None

        def get(self, _model, _draft_id):
            return draft

        def add(self, row):
            self.added.append(row)

        def flush(self):
            return None

    draft = SimpleNamespace(
        id="draft-1",
        status=ReviewStatus.READY_TO_PUBLISH.value,
        source_item=SimpleNamespace(
            id="source-1", source_kind="github", external_id="example/project"
        ),
        published_platform=None,
        published_url=None,
        published_at=None,
    )
    session = FakeSession()

    saved = ContentRepository(session).record_manual_publication(
        "draft-1",
        ManualPublicationCommand(
            platform="小红书",
            published_url="https://www.xiaohongshu.com/explore/example",
            idempotency_key="publication-key-1",
        ),
    )

    assert saved.status == ReviewStatus.PUBLISHED.value
    assert saved.published_platform == "小红书"
    assert saved.published_url == "https://www.xiaohongshu.com/explore/example"
    assert any(isinstance(row, PublicationRecordRow) for row in session.added)
    assert any(isinstance(row, ProjectIntroductionRow) for row in session.added)


def test_manual_publication_rejects_draft_that_is_not_approved():
    class FakeSession:
        def scalar(self, _statement):
            return None

        def get(self, _model, _draft_id):
            return SimpleNamespace(id="draft-2", status="pending_review")

    with pytest.raises(InvalidReviewTransition, match="审核通过"):
        ContentRepository(FakeSession()).record_manual_publication(
            "draft-2",
            ManualPublicationCommand(
                published_url="https://example.com/post/1",
                idempotency_key="publication-key-2",
            ),
        )


def test_wechat_publication_preparation_keeps_one_attempt_for_one_content_draft():
    class FakeSession:
        def __init__(self) -> None:
            self.added = []

        def get(self, _model, _id):
            return SimpleNamespace(id="draft-3", status=ReviewStatus.READY_TO_PUBLISH.value)

        def scalar(self, _statement):
            # 该草稿尚无投递记录，仓储会新建一条审计记录。
            return None

        def add(self, row):
            self.added.append(row)

        def flush(self):
            return None

    session = FakeSession()
    repository = ContentRepository(session)

    first = repository.save_wechat_publication(
        "draft-3", "cover-1", "wechat-cover-1", ["inline-1"], ["https://example.com/1.png"]
    )
    assert len(session.added) == 1
    assert all(isinstance(row, WechatPublicationJobRow) for row in session.added)
    assert [row.draft_id for row in session.added] == ["draft-3"]


def test_wechat_draftbox_success_is_terminal_and_records_source_deduplication():
    draft = SimpleNamespace(
        id="draft-4",
        status=ReviewStatus.READY_TO_PUBLISH.value,
        source_item=SimpleNamespace(id="source-4", source_kind="rss", external_id="release-4"),
    )
    job = SimpleNamespace(
        id="job-4", draft_id="draft-4", wechat_draft_media_id=None,
        state="cover_uploaded", error_message="old error",
    )

    class FakeSession:
        def __init__(self) -> None:
            self.added = []

        def get(self, model, _id):
            return job if model is WechatPublicationJobRow else draft

        def scalar(self, _statement):
            return None

        def add(self, row):
            self.added.append(row)

        def flush(self):
            return None

    saved = ContentRepository(FakeSession()).mark_wechat_draft_created("job-4", "media-4")

    assert saved.state == "draft_created"
    assert saved.wechat_draft_media_id == "media-4"
    assert draft.status == ReviewStatus.DRAFTBOX_CREATED.value
