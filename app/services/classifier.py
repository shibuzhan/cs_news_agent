from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import ContentCategory, NormalizedItem, SourceKind


@dataclass(frozen=True)
class Classification:
    category: ContentCategory
    confidence: float
    reason: str


KEYWORDS: list[tuple[ContentCategory, tuple[str, ...]]] = [
    (ContentCategory.PRODUCT_UPDATE, ("release", "launch", "version", "update", "发布", "更新", "版本")),
    (ContentCategory.RESEARCH, ("paper", "research", "benchmark", "model", "论文", "研究", "基准")),
    (ContentCategory.OPEN_SOURCE, ("github", "repository", "open source", "开源", "仓库")),
    (ContentCategory.INDUSTRY_NEWS, ("company", "funding", "acquire", "industry", "公司", "融资", "收购")),
]


def classify_item(item: NormalizedItem) -> Classification:
    if item.source_kind == SourceKind.ARXIV:
        return Classification(ContentCategory.RESEARCH, 0.99, "arXiv 来源")
    if item.source_kind == SourceKind.GITHUB:
        return Classification(ContentCategory.OPEN_SOURCE, 0.99, "GitHub Trending 来源")

    text = f"{item.title} {item.summary}".casefold()
    for category, keywords in KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return Classification(category, 0.82, "命中类别关键词")
    if item.source_kind in {SourceKind.HACKER_NEWS, SourceKind.RSS}:
        return Classification(ContentCategory.INDUSTRY_NEWS, 0.65, "资讯源默认类别")
    return Classification(ContentCategory.NEEDS_REVIEW, 0.3, "缺少足够分类信号")
