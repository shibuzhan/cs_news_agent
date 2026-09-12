from __future__ import annotations

from io import BytesIO

import pytest

from app.agents.chat_agent import ChatAgent
from app.config import Settings
from app.services.attachments import (
    AgentWorkspaceStore,
    AttachmentAccessError,
    AttachmentLinkSigner,
    AttachmentValidationError,
    PrivateAttachmentStore,
    decode_text_attachment,
    validate_image_attachment,
    validate_text_attachment,
)


def test_attachment_validation_only_allows_small_text_files() -> None:
    settings = Settings(attachment_max_bytes=10)

    assert validate_text_attachment("note.md", b"hello", settings) == ".md"
    with pytest.raises(AttachmentValidationError, match="仅支持"):
        validate_text_attachment("note.pdf", b"hello", settings)
    with pytest.raises(AttachmentValidationError, match="不能超过"):
        validate_text_attachment("note.txt", b"01234567890", settings)


def test_attachment_text_decoder_accepts_gb18030() -> None:
    assert decode_text_attachment("中文附件".encode("gb18030")) == "中文附件"


def test_publication_image_validation_rejects_text_and_wrong_mime_type() -> None:
    settings = Settings(attachment_max_bytes=10)

    assert validate_image_attachment("cover.png", b"image", "image/png", settings) == ".png"
    with pytest.raises(AttachmentValidationError, match="发布图片"):
        validate_image_attachment("cover.txt", b"text", "text/plain", settings)
    with pytest.raises(AttachmentValidationError, match="发布图片"):
        validate_image_attachment("cover.jpg", b"image", "image/png", settings)


def test_signed_attachment_link_cannot_be_reused_for_another_attachment() -> None:
    settings = Settings(attachment_download_secret="test-secret", attachment_link_ttl_seconds=60)
    signer = AttachmentLinkSigner(settings)
    token = signer.issue("attachment-a")

    signer.verify("attachment-a", token)
    with pytest.raises(AttachmentAccessError):
        signer.verify("attachment-b", token)
    with pytest.raises(AttachmentAccessError):
        signer.verify("attachment-a", "not-a-token")


class FakeObject:
    def __init__(self, content: bytes):
        self.content = content
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self.content

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class FakeMinio:
    def __init__(self):
        self.has_bucket = False
        self.stored: dict[str, bytes] = {}

    def bucket_exists(self, _bucket: str) -> bool:
        return self.has_bucket

    def make_bucket(self, _bucket: str) -> None:
        self.has_bucket = True

    def put_object(self, _bucket: str, object_key: str, data: BytesIO, **_kwargs: object) -> None:
        self.stored[object_key] = data.read()

    def get_object(self, _bucket: str, object_key: str) -> FakeObject:
        return FakeObject(self.stored[object_key])

    def remove_object(self, _bucket: str, object_key: str) -> None:
        del self.stored[object_key]


def test_private_store_generates_non_user_controlled_object_key() -> None:
    client = FakeMinio()
    store = PrivateAttachmentStore(Settings(), client=client)  # type: ignore[arg-type]

    object_key, content_hash = store.upload("../../brief.txt", b"private text", "text/plain")

    assert object_key.startswith("attachments/")
    assert ".." not in object_key
    assert len(content_hash) == 64
    assert client.has_bucket is True
    assert store.read(object_key) == b"private text"
    store.delete(object_key)
    assert object_key not in client.stored


def test_agent_workspace_is_session_scoped_and_rejects_path_traversal(tmp_path) -> None:
    workspace = AgentWorkspaceStore(Settings(agent_workspace_dir=str(tmp_path)))
    workspace.write("session-a", "attachment-a", "brief.md", b"private text")

    assert workspace.read("session-a", "attachment-a", "brief.md") == b"private text"
    with pytest.raises(AttachmentAccessError):
        workspace.read("session-a", "attachment-a", "../brief.md")

    workspace.delete_session("session-a")
    with pytest.raises(AttachmentAccessError):
        workspace.read("session-a", "attachment-a", "brief.md")


def test_chat_agent_requires_an_explicit_attachment_generation_instruction() -> None:
    agent = ChatAgent()

    assert agent.requests_attachment_extraction("提取附件并生成待审核草稿", "attachment-id")
    assert not agent.requests_attachment_extraction("请看看这个附件", "attachment-id")
    assert not agent.requests_attachment_extraction("提取附件并生成待审核草稿", None)
