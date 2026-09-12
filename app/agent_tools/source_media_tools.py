"""来源图片 Tool：列出本条来源可用的图片，并把选中的图下载入库、绑定到草稿。

来源范围（用户确认的策略）：**项目仓库自带的图**（README 内链接）+ **项目官方页面/文档页的图**
（后者经 Exa `web_fetch_exa` 抓取，会消耗检索配额）。第三方文章里的图不使用。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from langchain_core.tools import tool

from app.config import Settings, get_settings
from app.domain.models import ReviewStatus
from app.services.source_media import (
    DownloadedImage,
    SourceImage,
    SourceImageError,
    extract_image_urls,
    raw_github_base,
    validate_downloaded_image,
)
from app.services.source_snapshots import DraftSourceSnapshotStore
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.source_media_tools")

EDITABLE_STATUSES = {ReviewStatus.PENDING_REVIEW.value, ReviewStatus.NEEDS_REVISION.value}
MAX_PLACEMENT = 20
SOURCE_IMAGE_MAX_BYTES = 8 * 1024 * 1024


def _resolve_draft(repository: ContentRepository, session_id: str, draft_id: str) -> tuple[Any | None, str]:
    if draft_id:
        try:
            draft = repository.get_draft(draft_id)
        except Exception:
            return None, "找不到这篇草稿。"
        run = repository.find_generation_run_for_draft(draft_id)
        if run is None or run.session_id != session_id:
            return None, "该草稿不属于本会话，不能在这里操作。"
    else:
        memory = repository.get_chat_session_memory(session_id)
        if not memory.active_draft_id:
            return None, "本会话还没有当前文章：请先明确指定要操作的草稿。"
        draft = repository.get_draft(memory.active_draft_id)
    if draft.status not in EDITABLE_STATUSES:
        return None, "当前文章不在可编辑状态（待审核或需修改），不能调整图片。"
    return draft, ""


def _readme_text(settings: Settings, repository: ContentRepository, draft: Any) -> str:
    """优先取会话来源快照；已删除时退回来源条目正文。"""
    source = draft.source_item
    try:
        snapshot = DraftSourceSnapshotStore(settings, repository).restore_github_readme(draft.id, source)
        if snapshot is not None and snapshot.content:
            return snapshot.content
    except Exception as exc:  # 快照读取失败不应阻断取图
        logger.warning("source_media_snapshot_failed draft_id=%s error_type=%s", draft.id, type(exc).__name__)
    return str(source.content or "")


async def _download_image(client: httpx.AsyncClient, image: SourceImage) -> DownloadedImage:
    response = await client.get(image.url)
    if response.status_code != 200:
        raise SourceImageError(f"图片下载失败（HTTP {response.status_code}）")
    return validate_downloaded_image(
        image.url, response.content, response.headers.get("content-type", ""), max_bytes=SOURCE_IMAGE_MAX_BYTES
    )


def build_source_media_tools(session_id: str):
    """构造来源图片 Tool（列出候选 / 下载并绑定）。"""

    @tool("list_source_images")
    async def list_source_images(draft_id: str = "", include_official_site: bool = False) -> dict[str, Any]:
        """列出当前文章可用的真实图片来源：项目 README 自带的截图，以及可选的官方页面图。

        include_official_site=true 时会用联网检索抓项目官网/文档页（消耗一次检索配额）。
        """
        settings = get_settings()
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id)
            if draft is None:
                return {"status": "rejected", "reason": reason}
            readme = _readme_text(settings, repository, draft)
            base = raw_github_base(draft.source_url or "") or (draft.source_url or "")
            images = extract_image_urls(readme, base_url=base, origin="readme")
            if include_official_site:
                from app.tools.search_tools import ExaMcpSearchError, ExaMcpSearchTool

                try:
                    fetched = await ExaMcpSearchTool(settings).fetch([draft.source_url])
                    for entry in fetched:
                        images.extend(
                            extract_image_urls(str(entry.get("content") or ""), base_url=draft.source_url or "", origin="official_site")
                        )
                except ExaMcpSearchError as exc:
                    logger.warning("source_media_official_site_failed draft_id=%s", draft.id)
                    return {
                        "status": "partial",
                        "reason": str(exc),
                        "images": [item.__dict__ for item in images],
                    }
            return {
                "status": "ok",
                "draft_id": draft.id,
                "readme_chars": len(readme),
                "count": len(images),
                "images": [item.__dict__ for item in images[:20]],
            }

    @tool("attach_source_image")
    async def attach_source_image(
        url: str, purpose: str = "inline", placement_after_paragraph: int = 1, draft_id: str = ""
    ) -> dict[str, Any]:
        """把一张来源图片（README 或官方页面的图）下载并绑定到当前文章：purpose 为 cover 或 inline。

        只接受项目仓库与官方页面的图；图片会存入私有素材库并标注来源，不会上传或发表。
        """
        settings = get_settings()
        resolved_purpose = purpose if purpose in {"cover", "inline"} else "inline"
        placement = 0 if resolved_purpose == "cover" else max(0, min(int(placement_after_paragraph), MAX_PLACEMENT))
        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id)
            if draft is None:
                return {"status": "rejected", "reason": reason}
            readme = _readme_text(settings, repository, draft)
            base = raw_github_base(draft.source_url or "") or (draft.source_url or "")
            allowed = {item.url for item in extract_image_urls(readme, base_url=base, origin="readme")}
            if url not in allowed:
                return {
                    "status": "rejected",
                    "reason": "只允许使用来源 README 或官方页面里的图片；该链接不在候选里。",
                }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=60), follow_redirects=True) as client:
                downloaded = await _download_image(client, SourceImage(url=url, alt="", origin="readme"))
        except SourceImageError as exc:
            return {"status": "rejected", "reason": str(exc)}
        except Exception as exc:
            logger.warning("source_media_download_failed url_host=%s error_type=%s", url.split("/")[2] if "//" in url else "-", type(exc).__name__)
            return {"status": "rejected", "reason": "图片下载失败，请换一张或改用生成配图。"}
        from app.services.attachments import PrivateAttachmentStore
        from app.tools.image_generation import validate_image_attachment

        with SessionLocal() as session:
            repository = ContentRepository(session)
            draft, reason = _resolve_draft(repository, session_id, draft_id)
            if draft is None:
                return {"status": "rejected", "reason": reason}
            try:
                validate_image_attachment(
                    downloaded.filename, downloaded.content, downloaded.content_type, settings
                )
            except Exception as exc:
                return {"status": "rejected", "reason": f"图片校验未通过：{exc}"}
            object_key, content_hash = PrivateAttachmentStore(settings).upload(
                downloaded.filename, downloaded.content, downloaded.content_type
            )
            asset = repository.create_publication_asset(
                original_name=downloaded.filename,
                content_type=downloaded.content_type,
                object_key=object_key,
                size_bytes=len(downloaded.content),
                sha256=content_hash,
            )
            repository.bind_publication_asset(draft.id, asset.id)
            illustration = repository.create_draft_illustration(draft.id, asset.id, resolved_purpose, placement)
            session.commit()
            logger.info(
                "source_image_attached draft_id=%s asset_id=%s purpose=%s bytes=%s",
                draft.id, asset.id, resolved_purpose, len(downloaded.content),
            )
            return {
                "status": "done",
                "draft_id": draft.id,
                "draft_title": (draft.title_options_json or ["当前草稿"])[0],
                "illustration_id": illustration.id,
                "asset_id": asset.id,
                "purpose": resolved_purpose,
                "placement_after_paragraph": placement,
                "source_url": url,
                "message": f"已把来源图片绑定为{'封面' if resolved_purpose == 'cover' else f'第 {placement} 段后的正文插图'}；图片来自 {url}，请在正文中标注来源。",
            }

    return [list_source_images, attach_source_image]
