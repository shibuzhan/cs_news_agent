from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from minio import Minio
from minio.error import S3Error

from app.config import Settings


ALLOWED_TEXT_EXTENSIONS = {".txt", ".md", ".csv"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


class AttachmentError(RuntimeError):
    pass


class AttachmentValidationError(AttachmentError):
    pass


class AttachmentAccessError(AttachmentError):
    pass


def validate_attachment(
    filename: str, content: bytes, settings: Settings, max_bytes: int | None = None
) -> str:
    limit = settings.attachment_max_bytes if max_bytes is None else max_bytes
    suffix = Path(filename).suffix.casefold()
    if suffix not in ALLOWED_TEXT_EXTENSIONS | ALLOWED_IMAGE_EXTENSIONS:
        raise AttachmentValidationError("仅支持 .txt、.md、.csv、.jpg、.jpeg、.png 文件")
    if not content:
        raise AttachmentValidationError("不允许上传空文件")
    if len(content) > limit:
        raise AttachmentValidationError(f"附件不能超过 {limit // (1024 * 1024)} MB")
    return suffix


def validate_text_attachment(filename: str, content: bytes, settings: Settings) -> str:
    suffix = validate_attachment(filename, content, settings)
    if suffix not in ALLOWED_TEXT_EXTENSIONS:
        raise AttachmentValidationError("该操作仅支持 .txt、.md、.csv 文本文件")
    return suffix


def validate_image_attachment(
    filename: str, content: bytes, content_type: str, settings: Settings, max_bytes: int | None = None
) -> str:
    suffix = validate_attachment(filename, content, settings, max_bytes)
    expected_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    if suffix not in expected_types or content_type != expected_types.get(suffix):
        raise AttachmentValidationError("发布图片仅支持 JPG、JPEG、PNG 文件")
    return suffix


def decode_text_attachment(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AttachmentValidationError("附件编码不受支持，请使用 UTF-8 或 GB18030")


class PrivateAttachmentStore:
    """私有 MinIO 桶；应用层负责鉴权和短时下载链接。"""

    def __init__(self, settings: Settings, client: Minio | None = None):
        self.settings = settings
        self.client = client or Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    def upload(
        self, filename: str, content: bytes, content_type: str, max_bytes: int | None = None
    ) -> tuple[str, str]:
        suffix = validate_attachment(filename, content, self.settings, max_bytes)
        try:
            self._ensure_private_bucket()
            object_key = f"attachments/{uuid4()}{suffix}"
            self.client.put_object(
                self.settings.minio_bucket,
                object_key,
                BytesIO(content),
                length=len(content),
                content_type=content_type or "text/plain",
            )
        except S3Error as exc:
            raise AttachmentError("附件存储暂时不可用") from exc
        return object_key, hashlib.sha256(content).hexdigest()

    def read(self, object_key: str) -> bytes:
        try:
            response = self.client.get_object(self.settings.minio_bucket, object_key)
        except S3Error as exc:
            raise AttachmentError("附件存储暂时不可用") from exc
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def upload_internal_text_snapshot(self, content: bytes) -> tuple[str, str]:
        """保存服务端获取的来源正文，不复用用户附件名或公开下载能力。"""
        if not content:
            raise AttachmentValidationError("来源正文快照不能为空")
        if len(content) > self.settings.source_response_max_bytes:
            raise AttachmentValidationError("来源正文快照超过大小上限")
        try:
            self._ensure_private_bucket()
            object_key = f"source-snapshots/{uuid4()}.md"
            self.client.put_object(
                self.settings.minio_bucket,
                object_key,
                BytesIO(content),
                length=len(content),
                content_type="text/markdown; charset=utf-8",
            )
        except S3Error as exc:
            raise AttachmentError("来源正文快照存储暂时不可用") from exc
        return object_key, hashlib.sha256(content).hexdigest()

    def delete(self, object_key: str) -> None:
        try:
            self.client.remove_object(self.settings.minio_bucket, object_key)
        except S3Error as exc:
            raise AttachmentError("附件删除暂时不可用") from exc

    def _ensure_private_bucket(self) -> None:
        if not self.client.bucket_exists(self.settings.minio_bucket):
            self.client.make_bucket(self.settings.minio_bucket)


class AgentWorkspaceStore:
    """会话隔离的持久工作区；不接受用户提供的文件系统路径。"""

    def __init__(self, settings: Settings):
        self.root = Path(settings.agent_workspace_dir).resolve()

    def write(self, session_id: str, attachment_id: str, filename: str, content: bytes) -> None:
        path = self._path(session_id, attachment_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def read(self, session_id: str, attachment_id: str, filename: str) -> bytes:
        path = self._path(session_id, attachment_id, filename)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise AttachmentAccessError("附件工作区副本不存在") from exc

    def delete_session(self, session_id: str) -> None:
        session_path = (self.root / session_id).resolve()
        if session_path.parent != self.root or not session_path.exists():
            return
        for path in sorted(session_path.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        session_path.rmdir()

    def _path(self, session_id: str, attachment_id: str, filename: str) -> Path:
        safe_name = Path(filename).name
        if not safe_name or safe_name != filename or any(part in {"", ".", ".."} for part in (session_id, attachment_id)):
            raise AttachmentAccessError("附件工作区路径无效")
        path = (self.root / session_id / attachment_id / safe_name).resolve()
        if self.root not in path.parents:
            raise AttachmentAccessError("附件工作区路径无效")
        return path


class AttachmentLinkSigner:
    def __init__(self, settings: Settings):
        self.secret = settings.attachment_download_secret.encode("utf-8")
        self.ttl_seconds = settings.attachment_link_ttl_seconds

    def issue(self, attachment_id: str) -> str:
        expires_at = int(
            (datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)).timestamp()
        )
        payload = f"{attachment_id}:{expires_at}".encode("utf-8")
        signature = hmac.new(self.secret, payload, hashlib.sha256).digest()
        encoded_payload = base64.urlsafe_b64encode(payload).decode("ascii")
        encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii")
        return f"{encoded_payload}.{encoded_signature}"

    def verify(self, attachment_id: str, token: str) -> None:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            payload = base64.urlsafe_b64decode(encoded_payload.encode("ascii"))
            signature = base64.urlsafe_b64decode(encoded_signature.encode("ascii"))
            token_attachment_id, expires_at = payload.decode("utf-8").rsplit(":", 1)
            expires_at_int = int(expires_at)
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise AttachmentAccessError("下载链接无效") from exc
        expected = hmac.new(self.secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise AttachmentAccessError("下载链接无效")
        if token_attachment_id != attachment_id or expires_at_int < int(
            datetime.now(UTC).timestamp()
        ):
            raise AttachmentAccessError("下载链接已失效")
