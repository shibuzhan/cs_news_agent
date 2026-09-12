from __future__ import annotations

from dataclasses import dataclass

from app.services.attachments import (
    AgentWorkspaceStore,
    AttachmentAccessError,
    PrivateAttachmentStore,
    decode_text_attachment,
)
from app.storage.tables import AttachmentRow


class WorkspaceAccessError(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceTextFile:
    attachment_id: str
    filename: str
    content: str


class SessionWorkspaceTool:
    """只读取当前会话已登记附件；不接收或解析任意本机路径。"""

    def __init__(self, store: PrivateAttachmentStore, workspace: AgentWorkspaceStore | None = None):
        self.store = store
        self.workspace = workspace

    def read_text(self, session_id: str, attachment: AttachmentRow) -> WorkspaceTextFile:
        if attachment.session_id != session_id:
            raise WorkspaceAccessError("文件不属于当前会话")
        if self.workspace:
            try:
                content = self.workspace.read(session_id, attachment.id, attachment.original_name)
            except AttachmentAccessError:
                # 历史附件没有副本时，仅在已进入明确授权的 Tool 后才补建。
                content = self.store.read(attachment.object_key)
                self.workspace.write(session_id, attachment.id, attachment.original_name, content)
        else:
            content = self.store.read(attachment.object_key)
        return WorkspaceTextFile(
            attachment_id=attachment.id,
            filename=attachment.original_name,
            content=decode_text_attachment(content),
        )

    def write_text(self, filename: str, content: str) -> tuple[str, str, int]:
        object_key, content_hash = self.store.upload(filename, content.encode("utf-8"), "text/markdown")
        return object_key, content_hash, len(content.encode("utf-8"))
