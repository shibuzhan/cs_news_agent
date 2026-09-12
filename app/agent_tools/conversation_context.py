from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from app.domain.models import ReviewStatus
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository, DraftNotFound


EDITABLE_STATUSES = {ReviewStatus.PENDING_REVIEW.value, ReviewStatus.NEEDS_REVISION.value}


def _draft_view(draft: Any) -> dict[str, str]:
    return {
        "id": draft.id,
        "title": (draft.title_options_json or ["未命名草稿"])[0],
        "status": draft.status,
    }


def get_conversation_context_snapshot(session_id: str) -> dict[str, Any]:
    """读取可安全注入会话 Agent 的小型上下文快照，不暴露仓储或全局数据。"""
    with SessionLocal() as session:
        repository = ContentRepository(session)
        memory = repository.get_chat_session_memory(session_id)
        active_draft = None
        if memory.active_draft_id:
            try:
                active_draft = _draft_view(repository.get_draft(memory.active_draft_id))
            except DraftNotFound:
                repository.update_chat_session_memory(session_id, clear_draft=True)
                session.commit()
        attachment = None
        if memory.active_attachment_id:
            try:
                row = repository.get_attachment(memory.active_attachment_id)
                attachment = {"id": row.id, "name": row.original_name, "content_type": row.content_type}
            except Exception:
                repository.update_chat_session_memory(session_id, clear_attachment=True)
                session.commit()
        recent_messages = [
            {"role": message.role, "content": message.content[:600]}
            for message in repository.list_chat_messages(session_id)[-8:]
        ]
        return {
            "active_draft": active_draft,
            "active_attachment": attachment,
            "summary": memory.summary,
            "recent_messages": recent_messages,
        }


def build_conversation_context_tools(session_id: str):
    """构造仅作用于一个聊天会话的 Tool，不暴露仓储、路径或外部客户端。"""

    @tool("get_current_conversation_context")
    def get_current_conversation_context() -> dict[str, Any]:
        """读取本会话草稿、附件、最近任务摘要与最近消息；用于理解“这篇”“新增为零”“刚才结果”等指代。"""
        return get_conversation_context_snapshot(session_id)

    @tool("list_editable_drafts")
    def list_editable_drafts() -> list[dict[str, str]]:
        """列出最近可继续编辑的草稿。用户未明确指定文章时先用此 Tool 核对，不能猜测全局第一篇草稿。"""
        with SessionLocal() as session:
            repository = ContentRepository(session)
            return [_draft_view(row) for row in repository.list_drafts(limit=20) if row.status in EDITABLE_STATUSES]

    @tool("set_current_draft")
    def set_current_draft(draft_id: str) -> dict[str, str]:
        """在用户已明确选择草稿后，将其设置为本会话当前文章。不会修改草稿正文、审核状态或发布状态。"""
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft = repository.get_draft(draft_id)
            if draft.status not in EDITABLE_STATUSES:
                return {"status": "rejected", "reason": "该草稿当前不可编辑，不能设为会话当前文章。"}
            repository.update_chat_session_memory(session_id, active_draft_id=draft.id)
            session.commit()
            return {"status": "selected", **_draft_view(draft)}

    return [get_current_conversation_context, list_editable_drafts, set_current_draft]
