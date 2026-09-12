from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import feedparser

from app.domain.models import RawSourceItem, SourceKind
from app.sources.base import SourceCollectionError, SourceCollector


class RssCollector(SourceCollector):
    name = "官方 RSS"

    def __init__(self, client, feed_urls: list[str]):
        super().__init__(client)
        self.feed_urls = feed_urls

    @staticmethod
    def parse_feed(xml: str, feed_url: str, limit: int) -> list[RawSourceItem]:
        feed = feedparser.parse(xml)
        if getattr(feed, "bozo", False) and not feed.entries:
            raise SourceCollectionError(f"RSS 无法解析：{feed_url}")
        source_name = feed.feed.get("title") or feed_url
        items: list[RawSourceItem] = []
        for entry in feed.entries[:limit]:
            link = entry.get("link")
            title = entry.get("title", "")
            if not link or not title:
                continue
            parsed = getattr(entry, "published_parsed", None) or getattr(
                entry, "updated_parsed", None
            )
            published = datetime(*parsed[:6], tzinfo=UTC) if parsed else None
            summary = entry.get("summary", "")
            external_id = entry.get("id") or sha256(link.encode()).hexdigest()
            items.append(
                RawSourceItem(
                    source_kind=SourceKind.RSS,
                    external_id=external_id,
                    title=title,
                    url=link,
                    author=entry.get("author"),
                    published_at=published,
                    summary=summary,
                    content=summary,
                    source_name=source_name,
                    metadata={"feed_url": feed_url},
                )
            )
        return items

    async def collect(self, limit: int) -> list[RawSourceItem]:
        items: list[RawSourceItem] = []
        for feed_url in self.feed_urls:
            items.extend(self.parse_feed(await self.fetch_text(feed_url), feed_url, limit))
        return items[:limit]
