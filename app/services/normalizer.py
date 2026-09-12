from __future__ import annotations

import html
import re
from hashlib import sha256

from bs4 import BeautifulSoup

from app.domain.models import NormalizedItem, RawSourceItem


def clean_text(value: str, max_length: int) -> str:
    text = BeautifulSoup(html.unescape(value or ""), "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length]


def normalize_item(raw: RawSourceItem) -> NormalizedItem:
    title = clean_text(raw.title, 500)
    summary = clean_text(raw.summary, 5000)
    content = clean_text(raw.content, 500000)
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
