from __future__ import annotations

from app.services.attachments import PrivateAttachmentStore, decode_text_attachment
from app.storage.tables import AttachmentRow


class ExtractTextAttachmentTool:
    """仅由明确附件提取指令触发的文本读取 Tool。"""

    def __init__(self, store: PrivateAttachmentStore):
        self.store = store

    def invoke(self, attachment: AttachmentRow) -> str:
        return decode_text_attachment(self.store.read(attachment.object_key))[:30000]
