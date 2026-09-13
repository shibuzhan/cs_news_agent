"""微信公众号官方 API 的业务层公共类型与安全 HTML 渲染。"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.services.plain_text import is_source_footer_line, normalize_plain_text


class WechatOfficialAccountError(RuntimeError):
    """面向应用层的脱敏微信官方接口错误。"""

    def __init__(self, message: str, *, category: str = "unknown", provider_code: str | None = None):
        super().__init__(message)
        self.category = category
        self.provider_code = provider_code


@dataclass(frozen=True)
class WechatRemoteDraft:
    media_id: str
    title: str
    author: str
    created_at: str
    updated_at: str


def render_wechat_html(
    body: str,
    inline_image_urls: list[str | dict[str, Any]],
    *,
    footer_image_url: str | None = None,
    include_text_footer: bool = True,
) -> str:
    """将纯文本受审核文案转为最小安全 HTML；不解析 Markdown。

    插图只出现在正文开头或段落之间：位置被夹在 0 到“最后一段之前”，
    因此文末（最后一个正文段之后）只有**固定结尾图**。位置 0 表示正文开头。

    `footer_image_url` 给定时：文末追加该图，并按 `include_text_footer` 决定是否保留
    “原文标题／原文链接”这类文字尾注（用户要求“以固定结尾图取代原来的文字”）。
    """
    lines = [line.strip() for line in normalize_plain_text(body).splitlines() if line.strip()]
    if footer_image_url and not include_text_footer:
        lines = [line for line in lines if not is_source_footer_line(line)]
    content_paragraphs = sum(1 for line in lines if not is_source_footer_line(line))
    max_position = max(content_paragraphs - 1, 0)
    positioned: dict[int, list[str]] = {}
    for item in inline_image_urls:
        if isinstance(item, str):
            url, position = item, 0
        elif isinstance(item, dict):
            url = item.get("url", "")
            position = item.get("after_paragraph", 0)
        else:
            continue
        normalized_url = _wechat_inline_image_url(url)
        if normalized_url is None:
            continue
        if not isinstance(position, int) or position < 0:
            position = 0
        positioned.setdefault(min(position, max_position), []).append(normalized_url)

    blocks: list[str] = [f'<p><img src="{html.escape(url, quote=True)}" /></p>' for url in positioned.get(0, [])]
    paragraph_index = 0
    for line in lines:
        is_footer = is_source_footer_line(line)
        # 纯文本规范化会移除段首空白；微信正文逐段恢复两个全角空格缩进，来源行不缩进。
        prefix = "" if is_footer else "　　"
        blocks.append(f"<p>{prefix}{html.escape(line)}</p>")
        if is_footer:
            continue
        paragraph_index += 1
        for url in positioned.get(paragraph_index, []):
            blocks.append(f'<p><img src="{html.escape(url, quote=True)}" /></p>')
    footer_url = _wechat_inline_image_url(footer_image_url)
    if footer_url:
        blocks.append(f'<p><img src="{html.escape(footer_url, quote=True)}" /></p>')
    return "".join(blocks)


def _wechat_inline_image_url(value: object) -> str | None:
    """只升级微信 uploadimg 返回的 HTTP 地址，拒绝任意非 HTTPS 外链。"""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme == "https":
        return value
    # 微信 uploadimg 目前可能返回 http://mmbiz.qpic.cn/...；草稿 HTML 需要 HTTPS。
    if parsed.scheme == "http" and parsed.hostname == "mmbiz.qpic.cn":
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return None


def remote_drafts_from_payload(payload: dict[str, Any]) -> tuple[int, list[WechatRemoteDraft]]:
    """将官方 draft/batchget 的 JSON 限缩为发布页所需的安全字段。"""
    total = payload.get("total_count")
    items = payload.get("item")
    if not isinstance(total, int) or not isinstance(items, list):
        raise WechatOfficialAccountError("微信草稿箱列表返回格式异常", category="response")

    drafts: list[WechatRemoteDraft] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        media_id = item.get("media_id")
        content = item.get("content")
        if not isinstance(media_id, str) or not isinstance(content, dict):
            continue
        news_items = content.get("news_item")
        article = news_items[0] if isinstance(news_items, list) and news_items and isinstance(news_items[0], dict) else {}
        updated_at = _wechat_timestamp(item.get("update_time"))
        drafts.append(
            WechatRemoteDraft(
                media_id=media_id,
                title=_safe_text(article.get("title"), "未命名草稿"),
                author=_safe_text(article.get("author"), ""),
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
    return total, drafts


def _safe_text(value: object, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _wechat_timestamp(value: object) -> str:
    if not isinstance(value, int):
        return ""
    return datetime.fromtimestamp(value, tz=UTC).astimezone().isoformat(timespec="seconds")
