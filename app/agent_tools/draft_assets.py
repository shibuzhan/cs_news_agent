"""会话内的素材（配图）增删改查 Tool。

只作用于**本会话当前文章**：不接受任意草稿 id，也不上传、不发布、不删除素材文件本身，
只调整“草稿用哪些图、放在哪一段、哪张是封面”。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from app.domain.models import ReviewStatus
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository


# 配图可在待审核、需修改、审核通过、以及**已投递进公众号草稿箱**时调整：
# 已投递时改动会把投递标记为“已过期”，重新投递会原地覆盖远端草稿内容。
EDITABLE_STATUSES = {
    ReviewStatus.PENDING_REVIEW.value,
    ReviewStatus.NEEDS_REVISION.value,
    ReviewStatus.READY_TO_PUBLISH.value,
    ReviewStatus.DRAFTBOX_CREATED.value,
}
MAX_PLACEMENT = 20


def _resolve_draft(repository: ContentRepository, session_id: str) -> tuple[Any | None, str]:
    """只认本会话当前文章；没有或不可编辑时返回原因，由调用方原样转述。"""
    memory = repository.get_chat_session_memory(session_id)
    if not memory.active_draft_id:
        return None, "本会话还没有当前文章：请先明确指定要操作的草稿。"
    draft = repository.get_draft(memory.active_draft_id)
    if draft.status not in EDITABLE_STATUSES:
        return None, "当前文章已发布或已废弃，不能调整配图；已投递公众号草稿的文章可以继续改，改完重新投递会覆盖远端草稿。"
    return draft, ""


def _illustration_view(item: Any, repository: ContentRepository) -> dict[str, Any]:
    try:
        asset = repository.get_publication_asset(item.asset_id)
    except Exception:
        asset = None
    return {
        "illustration_id": item.id,
        "purpose": item.purpose,
        "placement_after_paragraph": item.placement_after_paragraph,
        "asset_id": item.asset_id,
        "asset_name": getattr(asset, "original_name", "") if asset else "",
    }


def build_draft_asset_tools(session_id: str):
    """构造只作用于本会话当前文章的素材 Tool。"""

    @tool("list_current_draft_illustrations")
    def list_current_draft_illustrations() -> dict[str, Any]:
        """列出本会话当前文章的配图：插图 id、用途（cover/inline）、所在段位与素材名。改图前先看这个。"""
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id)
            if draft is None:
                return {"status": "rejected", "reason": reason}
            items = repository.list_draft_illustrations(draft.id)
            return {
                "status": "ok",
                "draft": {"id": draft.id, "title": (draft.title_options_json or ["未命名草稿"])[0]},
                "illustrations": [_illustration_view(item, repository) for item in items],
            }

    @tool("set_current_draft_cover")
    def set_current_draft_cover(illustration_id: str) -> dict[str, Any]:
        """把本会话当前文章的某张已有插图设为封面。只会改变用途与位置，不新增或删除素材。"""
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id)
            if draft is None:
                return {"status": "rejected", "reason": reason}
            items = repository.list_draft_illustrations(draft.id)
            target = next((item for item in items if item.id == illustration_id), None)
            if target is None:
                return {"status": "rejected", "reason": "该插图不属于当前文章。"}
            for item in items:
                if item.purpose == "cover" and item.id != illustration_id:
                    repository.update_draft_illustration(draft.id, item.id, "inline", item.placement_after_paragraph or 1)
            updated = repository.update_draft_illustration(draft.id, target.id, "cover", 0)
            repository.invalidate_unfinished_wechat_publication_for_regeneration(
                draft.id, "当前文章配图已调整，原投递图片选择已失效；下次投递将只从当前保留图片重新确定。"
            )
            session.commit()
            return {"status": "ok", "illustration": _illustration_view(updated, repository)}

    @tool("move_current_draft_illustration")
    def move_current_draft_illustration(illustration_id: str, placement_after_paragraph: int) -> dict[str, Any]:
        """把本会话当前文章的一张正文插图移动到指定段落后（0 表示正文开头）。不改变素材本身。"""
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id)
            if draft is None:
                return {"status": "rejected", "reason": reason}
            items = repository.list_draft_illustrations(draft.id)
            target = next((item for item in items if item.id == illustration_id), None)
            if target is None:
                return {"status": "rejected", "reason": "该插图不属于当前文章。"}
            placement = max(0, min(int(placement_after_paragraph), MAX_PLACEMENT))
            updated = repository.update_draft_illustration(draft.id, target.id, "inline", placement)
            repository.invalidate_unfinished_wechat_publication_for_regeneration(
                draft.id, "当前文章配图已调整，原投递图片选择已失效；下次投递将只从当前保留图片重新确定。"
            )
            session.commit()
            return {"status": "ok", "illustration": _illustration_view(updated, repository)}

    @tool("delete_current_draft_illustration")
    def delete_current_draft_illustration(illustration_id: str) -> dict[str, Any]:
        """把一张图从本会话当前文章的配图里移除（素材文件保留在私有素材库，不删除文件）。"""
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id)
            if draft is None:
                return {"status": "rejected", "reason": reason}
            items = repository.list_draft_illustrations(draft.id)
            if not any(item.id == illustration_id for item in items):
                return {"status": "rejected", "reason": "该插图不属于当前文章。"}
            repository.delete_draft_illustration(draft.id, illustration_id)
            repository.invalidate_unfinished_wechat_publication_for_regeneration(
                draft.id, "当前文章配图已调整，原投递图片选择已失效；下次投递将只从当前保留图片重新确定。"
            )
            session.commit()
            return {"status": "ok", "deleted_id": illustration_id, "remaining": len(items) - 1}

    @tool("attach_existing_asset_to_current_draft")
    def attach_existing_asset_to_current_draft(
        asset_id: str, purpose: str = "inline", placement_after_paragraph: int = 0
    ) -> dict[str, Any]:
        """把私有素材库里已有的一张图绑定到本会话当前文章（purpose 为 cover 或 inline）。不新建、不上传、不发布。"""
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id)
            if draft is None:
                return {"status": "rejected", "reason": reason}
            resolved = purpose if purpose in {"cover", "inline"} else "inline"
            placement = max(0, min(int(placement_after_paragraph), MAX_PLACEMENT))
            if resolved == "cover":
                # 仓储层“已有封面就直接返回旧封面”，不先降级会静默失败。
                for item in items:
                    if item.purpose == "cover":
                        repository.update_draft_illustration(draft.id, item.id, "inline", 1)
            row = repository.create_draft_illustration(draft.id, asset_id, resolved, placement)
            repository.invalidate_unfinished_wechat_publication_for_regeneration(
                draft.id, "当前文章配图已调整，原投递图片选择已失效；下次投递将只从当前保留图片重新确定。"
            )
            session.commit()
            return {"status": "ok", "illustration": _illustration_view(row, repository)}

    return [
        list_current_draft_illustrations,
        set_current_draft_cover,
        move_current_draft_illustration,
        delete_current_draft_illustration,
        attach_existing_asset_to_current_draft,
    ]
