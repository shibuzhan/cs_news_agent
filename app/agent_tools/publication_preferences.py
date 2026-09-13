"""排版偏好的 Agent 工具：让 Agent 具备“长期修改”能力。

用户可以一句话让 Agent 改掉长期行为，例如：
- “封面也放进正文第一张” → `set_publication_preferences(cover_in_body=true)`
- “用这张图做每篇的结尾图” → `set_article_footer_image(...)`
- “结尾不要文字了” → `set_publication_preferences(footer_text_enabled=false)`

偏好写入 `app_settings` 表，容器重建也不会丢；之后每次投递排版都按它执行。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from app.config import get_settings
from app.domain.models import ReviewStatus
from app.services.attachments import AttachmentError, PrivateAttachmentStore
from app.services.source_media import SourceImageError
from app.services.publication_preferences import (
    add_style_note,
    load_publication_preferences,
    load_style_notes,
    remove_style_note,
    save_publication_preferences,
)
from app.services.wechat_official import WechatOfficialAccountError
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository
from app.tools.wechat_official_account import WechatOfficialAccountTool

logger = logging.getLogger(__name__)


def _describe(preferences, repository) -> dict[str, Any]:
    return {
        "cover_in_body": preferences.cover_in_body,
        "footer_image": preferences.footer_image_url or "（未设置）",
        "footer_image_asset_id": preferences.footer_image_asset_id or "（未设置）",
        "footer_text_enabled": preferences.footer_text_enabled,
        "style_notes": list(load_style_notes(repository)),
    }


def _impl_show_publication_preferences() -> dict[str, Any]:
    with SessionLocal() as session:
        repository = ContentRepository(session)
        preferences = load_publication_preferences(repository)
        described = _describe(preferences, repository)
    return {
        "status": "done",
        "preferences": described,
        "message": (
            f"当前长期排版偏好：封面{'也作为' if preferences.cover_in_body else '不作为'}正文首图；"
            f"固定结尾图{'已设置' if preferences.footer_image_url else '未设置'}；"
            f"文字尾注{'保留' if preferences.footer_text_enabled else '已由结尾图取代'}。"
            + (f"另有 {len(described['style_notes'])} 条长期写作偏好。" if described["style_notes"] else "")
        ),
    }


def _impl_set_publication_preferences(
    cover_in_body: bool | None = None,
    footer_text_enabled: bool | None = None,
) -> dict[str, Any]:
    if cover_in_body is None and footer_text_enabled is None:
        return {"status": "rejected", "reason": "请给出要修改的偏好（cover_in_body / footer_text_enabled）"}
    with SessionLocal() as session:
        repository = ContentRepository(session)
        preferences = save_publication_preferences(
            repository,
            cover_in_body=cover_in_body,
            footer_text_enabled=footer_text_enabled,
            updated_by="agent",
        )
        session.commit()
    return {
        "status": "done",
        "preferences": _describe(preferences, repository),
        "message": "已保存长期排版偏好；之后每次投递都会生效（已有草稿需要重新投递一次才会更新）。",
    }


def _resolve_footer_asset(session, repository: ContentRepository, asset_id: str, url: str) -> tuple[str, str]:
    """确定结尾图素材：优先用给定素材 id，其次把给定图片链接下载入库。"""
    settings = get_settings()
    if asset_id:
        asset = repository.get_publication_asset(asset_id)
        return asset.id, asset.original_name
    if not url:
        raise ValueError("请提供素材 id 或图片链接")
    import asyncio

    import httpx

    from app.agent_tools.source_media_tools import _download_image  # 复用同一套下载+魔数校验
    from app.services.source_media import SourceImage

    async def _fetch():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=25), follow_redirects=True) as client:
            return await _download_image(client, SourceImage(url=url, alt="", origin="footer"))

    downloaded = asyncio.run(_fetch())
    store = PrivateAttachmentStore(settings)
    object_key, content_hash = store.upload(downloaded.filename, downloaded.content, downloaded.content_type)
    asset = repository.create_publication_asset(
        original_name=f"footer-{downloaded.filename}",
        content_type=downloaded.content_type,
        object_key=object_key,
        size_bytes=len(downloaded.content),
        sha256=content_hash,
    )
    return asset.id, asset.original_name


def _impl_set_article_footer_image(asset_id: str = "", url: str = "") -> dict[str, Any]:
    settings = get_settings()
    with SessionLocal() as session:
        repository = ContentRepository(session)
        try:
            resolved_id, name = _resolve_footer_asset(session, repository, asset_id.strip(), url.strip())
        except (ValueError, AttachmentError, SourceImageError) as exc:
            return {"status": "rejected", "reason": f"结尾图不可用：{exc}"}
        asset = repository.get_publication_asset(resolved_id)
        try:
            content = PrivateAttachmentStore(settings).read(asset.object_key)
            import asyncio

            async def _upload() -> str:
                async with WechatOfficialAccountTool(settings) as client:
                    return await client.upload_inline_image(content, asset.original_name)

            inline_url = asyncio.run(_upload())
        except (AttachmentError, WechatOfficialAccountError, RuntimeError) as exc:
            return {"status": "failed", "reason": f"结尾图上传公众号失败：{exc}"}
        repository.set_app_setting("publication.footer_inline_url", inline_url, updated_by="agent")
        preferences = save_publication_preferences(
            repository,
            footer_image_url=inline_url,
            footer_image_asset_id=resolved_id,
            updated_by="agent",
        )
        session.commit()
    return {
        "status": "done",
        "footer_image": name,
        "preferences": _describe(preferences, repository),
        "message": (
            f"已把 {name} 设为每篇文章的固定结尾图，并默认不再输出文字尾注；"
            "之后每次投递都会自动加在文末（已有草稿需重新投递一次）。"
        ),
    }


def _impl_clear_article_footer_image() -> dict[str, Any]:
    with SessionLocal() as session:
        repository = ContentRepository(session)
        preferences = save_publication_preferences(
            repository, footer_image_url="", footer_text_enabled=True, updated_by="agent"
        )
        session.commit()
    return {"status": "done", "preferences": _describe(preferences, repository), "message": "已清除固定结尾图，文字尾注恢复。"}


def _impl_add_style_preference(text: str) -> dict[str, Any]:
    note = str(text or "").strip()
    if not note:
        return {"status": "rejected", "reason": "请给出这条长期偏好的内容"}
    with SessionLocal() as session:
        repository = ContentRepository(session)
        notes = add_style_note(repository, note)
        session.commit()
    return {
        "status": "done",
        "style_notes": list(notes),
        "message": (
            "已记录这条长期偏好，并会注入到**生成、改稿、审核**三处提示词；"
            "对已生成的草稿需要重新生成或重写才会带上。"
        ),
    }


def _impl_remove_style_preference(text: str) -> dict[str, Any]:
    target = str(text or "").strip()
    with SessionLocal() as session:
        repository = ContentRepository(session)
        notes = remove_style_note(repository, target)
        session.commit()
    return {"status": "done", "style_notes": list(notes), "message": "已移除该条长期偏好。"}


def build_publication_preference_tools(session_id: str):  # noqa: ARG001 - 与其它工具构造器一致
    from app.services.async_bridge import run_coroutine_sync  # noqa: F401 - 保持与其它工具一致的同步约定

    @tool("show_publication_preferences")
    def show_publication_preferences() -> dict[str, Any]:
        """查看长期排版偏好（封面是否作为正文首图、固定结尾图、是否保留文字尾注）。"""
        return _impl_show_publication_preferences()

    @tool("set_publication_preferences")
    def set_publication_preferences(
        cover_in_body: bool | None = None, footer_text_enabled: bool | None = None
    ) -> dict[str, Any]:
        """修改长期排版偏好：封面是否作为正文首图、是否保留文字尾注（长期生效）。"""
        return _impl_set_publication_preferences(
            cover_in_body=cover_in_body, footer_text_enabled=footer_text_enabled
        )

    @tool("set_article_footer_image")
    def set_article_footer_image(asset_id: str = "", url: str = "") -> dict[str, Any]:
        """设置每篇文章的固定结尾图（可用素材 id 或图片链接），并默认取代文字尾注。"""
        return _impl_set_article_footer_image(asset_id=asset_id, url=url)

    @tool("clear_article_footer_image")
    def clear_article_footer_image() -> dict[str, Any]:
        """清除固定结尾图，恢复文字尾注。"""
        return _impl_clear_article_footer_image()

    @tool("add_style_preference")
    def add_style_preference(text: str) -> dict[str, Any]:
        """新增一条**长期**写作偏好（自然语言），会注入生成/改稿/审核；例如“正文不要用问句标题”。"""
        return _impl_add_style_preference(text)

    @tool("remove_style_preference")
    def remove_style_preference(text: str) -> dict[str, Any]:
        """移除一条已记录的长期写作偏好（按原文精确匹配）。"""
        return _impl_remove_style_preference(text)

    return [
        show_publication_preferences,
        add_style_preference,
        remove_style_preference,
        set_publication_preferences,
        set_article_footer_image,
        clear_article_footer_image,
    ]
