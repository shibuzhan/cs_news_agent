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
from app.services.async_bridge import run_coroutine_sync
from app.services.source_snapshots import DraftSourceSnapshotStore
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.source_media_tools")

# 配图可在待审核、需修改、审核通过、以及**已投递进公众号草稿箱**时调整：
# 已投递时改动会把投递标记为“已过期”，重新投递会原地覆盖远端草稿内容。
EDITABLE_STATUSES = {
    ReviewStatus.PENDING_REVIEW.value,
    ReviewStatus.NEEDS_REVISION.value,
    ReviewStatus.READY_TO_PUBLISH.value,
    ReviewStatus.DRAFTBOX_CREATED.value,
}
MAX_PLACEMENT = 20
SOURCE_IMAGE_MAX_BYTES = 8 * 1024 * 1024
# 下载必须短：这些工具在对话请求内执行，不能占满请求时限。
SOURCE_IMAGE_TIMEOUT = httpx.Timeout(10, read=15)


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
        return None, "当前文章已发布或已废弃，不能调整配图；已投递公众号草稿的文章可以继续改，改完重新投递会覆盖远端草稿。"
    return draft, ""


def _readme_text(settings: Settings, repository: ContentRepository, draft: Any) -> str:
    """优先取会话来源快照；已删除时退回来源条目正文。"""
    source = draft.source_item
    try:
        # 快照存储要求 RawSourceItem（会读 .metadata 并 model_copy），
        # 直接传 ORM 行会抛 AttributeError，这里按字段重建一次。
        from app.domain.models import RawSourceItem

        item = RawSourceItem(
            source_kind=source.source_kind,
            external_id=source.external_id,
            title=source.title,
            url=source.url,
            source_name=source.source_name or "GitHub Trending",
            summary=source.summary or "",
            content=source.content or "",
        )
        snapshot = DraftSourceSnapshotStore(settings, repository).restore_github_readme(draft.id, item)
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


async def _live_readme_text(source_url: Any) -> str:
    """用 raw.githubusercontent 取实时 README（不走 API，不受 60 次/小时匿名配额限制）。

    存储副本可能过旧（例如仓库后来补了 docs/media 截图），允许清单需要它能兜底，
    否则用户点名的官方截图会被拒。
    """
    base = raw_github_base(source_url)
    if not base:
        return ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=20), follow_redirects=True) as client:
            response = await client.get(f"{base}README.md")
            return response.text if response.status_code == 200 else ""
    except Exception as exc:
        logger.warning("source_media_live_readme_failed error_type=%s", type(exc).__name__)
        return ""


def _demote_existing_covers(repository: ContentRepository, draft_id: str) -> list[str]:
    """把已有封面降级为正文插图，让新封面能真正生效。

    仓储层对 `purpose="cover"` 有“已存在则直接返回旧封面”的规则：不先降级就会出现
    “工具回报成功、但图并没有换成封面”的静默失败（真实故障：hero.png 未成为封面）。
    """
    demoted: list[str] = []
    for row in repository.list_draft_illustrations(draft_id):
        if row.purpose == "cover":
            repository.update_draft_illustration(draft_id, row.id, "inline", 1)
            demoted.append(row.id)
    return demoted


def build_source_media_tools(session_id: str):
    """构造来源图片 Tool（列出候选 / 下载并绑定）。"""

    async def _impl_list_source_images(draft_id: str = "", include_official_site: bool = False) -> dict[str, Any]:
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
            base = raw_github_base(draft.source_url) or str(draft.source_url or "")
            images = extract_image_urls(readme, base_url=base, origin="readme")
            if include_official_site:
                from app.tools.search_tools import ExaMcpSearchError, ExaMcpSearchTool

                try:
                    fetched = await ExaMcpSearchTool(settings).fetch([str(draft.source_url or "")])
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

    async def _impl_attach_source_image(
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
            base = raw_github_base(draft.source_url) or str(draft.source_url or "")
            allowed = {item.url for item in extract_image_urls(readme, base_url=base, origin="readme")}
            if url not in allowed:
                # 存储副本可能过旧（仓库后来补了 docs/media 截图）：用实时 README 兜底核对，
                # 仍然只允许仓库/官方页面的图片，不放宽来源范围。
                live = await _live_readme_text(draft.source_url)
                allowed |= {item.url for item in extract_image_urls(live, base_url=base, origin="readme")}
            if url not in allowed:
                return {
                    "status": "rejected",
                    "reason": "只允许使用来源 README 或官方页面里的图片；该链接不在候选里。",
                }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=15), follow_redirects=True) as client:
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
                # `source-` 前缀让素材选择器能区分“真实截图”与“AI 配图”（正文优先用真实图）。
                original_name=f"source-{downloaded.filename}",
                content_type=downloaded.content_type,
                object_key=object_key,
                size_bytes=len(downloaded.content),
                sha256=content_hash,
            )
            repository.bind_publication_asset(draft.id, asset.id)
            if resolved_purpose == "cover":
                _demote_existing_covers(repository, draft.id)
            illustration = repository.create_draft_illustration(draft.id, asset.id, resolved_purpose, placement)
            # 配图变了，旧的投递素材选择就不再成立：作废以免投递用了过期的图。
            repository.invalidate_unfinished_wechat_publication_for_regeneration(
                draft.id, "当前文章配图已调整，原投递图片选择已失效；下次投递将只从当前保留图片重新确定。"
            )
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

    # 会话 Agent 同步执行工具：异步实现需要同步外壳（否则 NotImplementedError）。
    @tool("list_source_images")
    def list_source_images(draft_id: str = "", include_official_site: bool = False) -> dict[str, Any]:
        """列出当前文章可用的真实图片来源：项目 README 自带的截图，以及可选的官方页面图。

        include_official_site=true 时会用联网检索抓项目官网/文档页（消耗一次检索配额）。
        """
        return run_coroutine_sync(
            _impl_list_source_images(draft_id=draft_id, include_official_site=include_official_site)
        )

    @tool("attach_source_image")
    def attach_source_image(
        url: str, purpose: str = "inline", placement_after_paragraph: int = 1, draft_id: str = ""
    ) -> dict[str, Any]:
        """把一张来源图片（README 或官方页面的图）下载并绑定到当前文章：purpose 为 cover 或 inline。

        只接受项目仓库与官方页面的图；图片会存入私有素材库并标注来源，不会上传或发表。
        """
        return run_coroutine_sync(
            _impl_attach_source_image(
                url=url, purpose=purpose,
                placement_after_paragraph=placement_after_paragraph, draft_id=draft_id,
            )
        )

    return [list_source_images, attach_source_image]
