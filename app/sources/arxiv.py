from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

import feedparser

from app.domain.models import RawSourceItem, SourceKind
from app.sources.base import SourceCollectionError, SourceCollector


class ArxivCollector(SourceCollector):
    name = "arXiv"
    endpoint = "https://export.arxiv.org/api/query"

    def __init__(self, client, categories: list[str]):
        super().__init__(client)
        self.categories = categories

    def build_url(self, limit: int) -> str:
        search_query = " OR ".join(f"cat:{category}" for category in self.categories)
        return f"{self.endpoint}?{urlencode({'search_query': search_query, 'start': 0, 'max_results': limit, 'sortBy': 'submittedDate', 'sortOrder': 'descending'})}"

    @staticmethod
    def parse_feed(xml: str, limit: int) -> list[RawSourceItem]:
        feed = feedparser.parse(xml)
        if getattr(feed, "bozo", False) and not feed.entries:
            raise SourceCollectionError("arXiv 返回了无法解析的 Atom 内容")
        items: list[RawSourceItem] = []
        for entry in feed.entries[:limit]:
            published = None
            parsed = getattr(entry, "published_parsed", None)
            if parsed:
                published = datetime(*parsed[:6], tzinfo=UTC)
            authors = ", ".join(
                author.get("name", "") for author in getattr(entry, "authors", [])
            )
            external_id = entry.get("id", "").rstrip("/").split("/")[-1]
            categories = [tag.get("term") for tag in getattr(entry, "tags", [])]
            items.append(
                RawSourceItem(
                    source_kind=SourceKind.ARXIV,
                    external_id=external_id,
                    title=entry.get("title", "").replace("\n", " "),
                    url=entry.get("link") or entry.get("id"),
                    author=authors or None,
                    published_at=published,
                    summary=entry.get("summary", ""),
                    content=entry.get("summary", ""),
                    source_name="arXiv",
                    metadata={"categories": categories},
                )
            )
        return items

    async def collect(self, limit: int) -> list[RawSourceItem]:
        return self.parse_feed(await self.fetch_text(self.build_url(limit)), limit)
