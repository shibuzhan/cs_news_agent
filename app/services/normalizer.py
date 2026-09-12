from __future__ import annotations

import html
import re
from hashlib import sha256

from bs4 import BeautifulSoup

from app.domain.models import NormalizedItem, RawSourceItem


def clean_text(value: str, max_length: int, keep_newlines: bool = False) -> str:
    """清洗来源文本。

    `keep_newlines=True` 用于来源正文：只折叠行内空白与连续空行，保留换行。
    正文一旦被压成一行，标题、列表与表格结构就全部丢失，标题分节与后续的证据挑选都无从谈起。
    """
    raw = BeautifulSoup(html.unescape(value or ""), "html.parser").get_text("\n" if keep_newlines else " ")
    if not keep_newlines:
        return re.sub(r"\s+", " ", raw).strip()[:max_length]
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return collapsed[:max_length]


def normalize_item(raw: RawSourceItem) -> NormalizedItem:
    title = clean_text(raw.title, 500)
    summary = clean_text(raw.summary, 5000)
    content = clean_text(raw.content, 500000, keep_newlines=True)
    fingerprint_material = f"{title.casefold()}\n{(summary or content)[:2000].casefold()}"
    return NormalizedItem(
        **raw.model_dump(exclude={"title", "summary", "content"}),
        title=title,
        summary=summary,
        content=content,
        content_hash=sha256(fingerprint_material.encode("utf-8")).hexdigest(),
    )


def has_meaningful_content(item: NormalizedItem) -> bool:
    return len(f"{item.title} {item.summary or item.content}".strip()) >= 20
