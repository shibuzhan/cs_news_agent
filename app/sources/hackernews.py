from __future__ import annotations

import asyncio
import ipaddress
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from app.domain.models import RawSourceItem, SourceKind
from app.services.article_extractor import extract_article_text
from app.sources.base import SourceCollectionError, SourceCollector


class HackerNewsCollector(SourceCollector):
    name = "Hacker News"
    endpoint = "https://hacker-news.firebaseio.com/v0"

    def __init__(
        self, client: httpx.AsyncClient, response_max_bytes: int = 1_000_000,
        content_max_chars: int = 500_000,
    ):
        super().__init__(client)
        self.response_max_bytes = response_max_bytes
        self.content_max_chars = content_max_chars

    @staticmethod
    def _is_public_http_url(value: str | None) -> bool:
        if not value:
            return False
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        hostname = parsed.hostname.casefold()
        if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
            return False
        try:
            return ipaddress.ip_address(hostname).is_global
        except ValueError:
            return True

    async def _fetch_external_body(self, url: str) -> tuple[str, str | None]:
        try:
            page = await self.fetch_text_limited(url, self.response_max_bytes)
            text = extract_article_text(page, self.content_max_chars)
            return text, None
        except SourceCollectionError as exc:
            return "", str(exc)

    async def _get_json(self, url: str):
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceCollectionError(f"Hacker News 采集失败：{exc}") from exc

    async def collect(self, limit: int) -> list[RawSourceItem]:
        ids = await self._get_json(f"{self.endpoint}/topstories.json")
        semaphore = asyncio.Semaphore(8)

        async def fetch_item(item_id: int):
            async with semaphore:
                return await self._get_json(f"{self.endpoint}/item/{item_id}.json")

        payloads = await asyncio.gather(*(fetch_item(item_id) for item_id in ids[:limit]))
        article_semaphore = asyncio.Semaphore(4)

        async def build_item(payload: dict) -> RawSourceItem | None:
            if not payload or payload.get("type") != "story" or not payload.get("title"):
                return None
            item_id = str(payload["id"])
            source_url = payload.get("url")
            hn_url = f"https://news.ycombinator.com/item?id={item_id}"
            hn_text = payload.get("text", "")
            content = hn_text
            metadata = {"hn_url": hn_url, "content_origin": "hn_text"}
            if self._is_public_http_url(source_url):
                async with article_semaphore:
                    external_text, fetch_error = await self._fetch_external_body(source_url)
                if external_text:
                    content = external_text
                    metadata["content_origin"] = "external_article"
                    metadata["article_fetch_status"] = "success"
                else:
                    metadata["article_fetch_status"] = "failed"
                    metadata["article_fetch_error"] = fetch_error or "正文为空"
            elif source_url:
                metadata["article_fetch_status"] = "skipped_unsafe_url"
            return RawSourceItem(
                source_kind=SourceKind.HACKER_NEWS,
                external_id=item_id,
                title=payload["title"],
                url=source_url or hn_url,
                author=payload.get("by"),
                published_at=datetime.fromtimestamp(payload["time"], UTC) if payload.get("time") else None,
                summary=content[:1200] if content else hn_text,
                content=content,
                source_name="Hacker News",
                metrics={"score": payload.get("score", 0), "comments": payload.get("descendants", 0)},
                metadata=metadata,
            )

        processed = await asyncio.gather(*(build_item(payload) for payload in payloads))
        items: list[RawSourceItem] = []
        items.extend(item for item in processed if item is not None)
        return items
