from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.models import DraftEdit, ReviewCommand, ReviewStatus
from app.storage.repositories import ContentRepository, InvalidReviewTransition
from app.storage.tables import ReviewEventRow


class FakeSession:
    def __init__(self, draft):
        self.draft = draft
        self.added = []

    def scalar(self, _statement):
        return None

    def get(self, _model, _draft_id):
        return self.draft

    def add(self, row):
        self.added.append(row)

    def flush(self):
        return None


def test_discard_preserves_draft_and_writes_an_audit_event() -> None:
    draft = SimpleNamespace(id="draft-1", status=ReviewStatus.PENDING_REVIEW.value)
    session = FakeSession(draft)

    saved = ContentRepository(session).review_draft(
        "draft-1",
        ReviewCommand(
            reviewer="运营人员",
            action="discard",
            note="选题不再采用",
            idempotency_key="discard-key-1",
        ),
    )

    assert saved is draft
    assert draft.status == ReviewStatus.DISCARDED.value
    event = next(row for row in session.added if isinstance(row, ReviewEventRow))
    assert event.action == "discard"
    assert event.note == "选题不再采用"


def test_discarded_draft_cannot_be_edited_or_reviewed_again() -> None:
    draft = SimpleNamespace(id="draft-2", status=ReviewStatus.DISCARDED.value)
    repository = ContentRepository(FakeSession(draft))

    with pytest.raises(InvalidReviewTransition, match="已废弃"):
        repository.edit_draft("draft-2", DraftEdit(body="不应被保存"))

    with pytest.raises(InvalidReviewTransition, match="已废弃"):
        repository.review_draft(
            "draft-2",
            ReviewCommand(
                reviewer="运营人员",
                action="approve",
                idempotency_key="discard-key-2",
            ),
        )


def test_delete_hides_draft_without_removing_its_audit_trail() -> None:
    draft = SimpleNamespace(id="draft-3", status=ReviewStatus.PENDING_REVIEW.value)
    session = FakeSession(draft)

    saved = ContentRepository(session).delete_draft("draft-3")

    assert saved is draft
    assert draft.status == ReviewStatus.DELETED.value
    event = next(row for row in session.added if isinstance(row, ReviewEventRow))
    assert event.action == "delete"
    assert "审核记录保留" in event.note


def test_revoke_approval_returns_draft_to_editable_revision_state() -> None:
    draft = SimpleNamespace(id="draft-5", status=ReviewStatus.READY_TO_PUBLISH.value)
    session = FakeSession(draft)

    saved = ContentRepository(session).review_draft(
        "draft-5",
        ReviewCommand(
            reviewer="运营人员",
            action="revoke",
            note="撤销审核后继续改进文案",
            idempotency_key="revoke-key-5",
        ),
    )

    assert saved is draft
    assert draft.status == ReviewStatus.NEEDS_REVISION.value
    event = next(row for row in session.added if isinstance(row, ReviewEventRow))
    assert event.action == "revoke"


def test_deleted_draft_cannot_be_edited_or_reviewed_again() -> None:
    draft = SimpleNamespace(id="draft-4", status=ReviewStatus.DELETED.value)
    repository = ContentRepository(FakeSession(draft))

    with pytest.raises(InvalidReviewTransition, match="已删除"):
        repository.edit_draft("draft-4", DraftEdit(body="不应被保存"))

    with pytest.raises(InvalidReviewTransition, match="已删除"):
        repository.review_draft(
            "draft-4",
            ReviewCommand(
                reviewer="运营人员",
                action="approve",
                idempotency_key="delete-key-4",
            ),
        )
