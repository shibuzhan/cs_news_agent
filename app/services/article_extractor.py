from __future__ import annotations

import re

from bs4 import BeautifulSoup


def extract_article_text(html: str, max_chars: int) -> str:
    """从公开文章 HTML 提取正文，保留段落但不执行页面中的任何内容。"""
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.select("script, style, noscript, nav, header, footer, aside, form, svg"):
        element.decompose()

    root = soup.select_one("article") or soup.select_one("main") or soup.body or soup
    blocks = [
        block.get_text(" ", strip=True)
        for block in root.select("h1, h2, h3, p, li, pre, blockquote")
    ]
    text = "\n\n".join(block for block in blocks if block)
    if not text:
        text = root.get_text(" ", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars]
