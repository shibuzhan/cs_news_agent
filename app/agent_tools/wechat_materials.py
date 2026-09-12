"""公众号永久素材（素材库图片）的盘点与清理。

真实问题：反复覆盖投递会让同一张封面在素材库里堆成多份，而永久素材**计入配额**
（图片类通常上限 5000 张）。这里提供两个工具：
- `list_wechat_materials`：只读盘点，标出**没有任何投递记录引用**的图片（可清理项）；
- `delete_wechat_material`：删除指定永久素材（破坏性操作，只删明确指定的 media_id）。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from app.config import get_settings
from app.services.wechat_official import WechatOfficialAccountError
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository
from app.tools.wechat_official_account import WechatOfficialAccountTool

logger = logging.getLogger(__name__)

MAX_MATERIALS = 200


def _referenced_media_ids(repository: ContentRepository) -> set[str]:
    """本系统所有投递记录引用过的封面 media_id。"""
    return {
        row.cover_media_id
        for row in repository.list_wechat_publications(limit=500)
        if getattr(row, "cover_media_id", None)
    }


async def _impl_list_wechat_materials(limit: int = 50) -> dict[str, Any]:
    settings = get_settings()
    count = max(1, min(int(limit), MAX_MATERIALS))
    with SessionLocal() as session:
        referenced = _referenced_media_ids(ContentRepository(session))
    try:
        async with WechatOfficialAccountTool(settings) as client:
            payload = await client.list_permanent_images(offset=0, count=count)
    except (WechatOfficialAccountError, RuntimeError) as exc:
        return {"status": "failed", "reason": f"读取素材库失败：{exc}"}
    items = payload.get("item") or []
    materials: list[dict[str, Any]] = []
    for item in items:
        media_id = str(item.get("media_id") or "")
        materials.append(
            {
                "media_id": media_id,
                "name": str(item.get("name") or ""),
                "update_time": item.get("update_time"),
                "referenced_by_delivery": media_id in referenced,
            }
        )
    unused = [item for item in materials if not item["referenced_by_delivery"]]
    logger.info(
        "wechat_materials_listed total=%s unused=%s", payload.get("total_count"), len(unused)
    )
    return {
        "status": "done",
        "total_in_library": payload.get("total_count"),
        "checked": len(materials),
        "unused_count": len(unused),
        "unused": unused[:50],
        "message": (
            f"素材库共 {payload.get('total_count')} 张图片，本次检查 {len(materials)} 张，"
            f"其中 {len(unused)} 张没有任何投递记录引用（可考虑清理）。"
            "删除会立即释放配额，但被已发表文章引用的图片不要删。"
        ),
    }


async def _impl_delete_wechat_material(media_id: str) -> dict[str, Any]:
    target = str(media_id or "").strip()
    if not target:
        return {"status": "rejected", "reason": "请提供要删除的素材 media_id"}
    settings = get_settings()
    with SessionLocal() as session:
        referenced = _referenced_media_ids(ContentRepository(session))
    if target in referenced:
        return {
            "status": "rejected",
            "reason": "该素材仍被投递记录引用（是某篇草稿的封面），删除会影响那篇草稿；请先重新投递换封面。",
        }
    try:
        async with WechatOfficialAccountTool(settings) as client:
            await client.delete_material(target)
    except (WechatOfficialAccountError, RuntimeError) as exc:
        return {"status": "failed", "reason": f"删除素材失败：{exc}"}
    logger.info("wechat_material_deleted media_id=%s", target[:12])
    return {"status": "done", "media_id": target, "message": "已从素材库删除该图片，配额已释放。"}


def build_wechat_material_tools(session_id: str):  # noqa: ARG001 - 与其它工具构造器保持一致
    @tool("list_wechat_materials")
    def list_wechat_materials(limit: int = 50) -> dict[str, Any]:
        """盘点公众号素材库图片：列出没有任何投递记录引用的图片（只读，不会删除任何东西）。"""
        from app.services.async_bridge import run_coroutine_sync

        return run_coroutine_sync(_impl_list_wechat_materials(limit=limit))

    @tool("delete_wechat_material")
    def delete_wechat_material(media_id: str) -> dict[str, Any]:
        """删除公众号素材库里的一张永久图片（破坏性操作，需明确给出 media_id）。"""
        from app.services.async_bridge import run_coroutine_sync

        return run_coroutine_sync(_impl_delete_wechat_material(media_id=media_id))

    return [list_wechat_materials, delete_wechat_material]
