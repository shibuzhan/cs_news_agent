from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

import httpx

from app.domain.models import RawSourceItem


logger = logging.getLogger("news_agent.source_http")


class SourceCollectionError(RuntimeError):
    """来源不可用或响应无法解析。"""


class SourceCollector(ABC):
    name: str

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        logger.info("source_http_started source=%s host=%s", self.name, urlsplit(url).hostname)
        try:
            response = await self.client.get(url, headers=self._headers(headers))
            response.raise_for_status()
            text = response.text
            logger.info(
                "source_http_finished source=%s host=%s characters=%s",
                self.name,
                urlsplit(url).hostname,
                len(text),
            )
            return text
        except httpx.HTTPError as exc:
            logger.warning(
                "source_http_failed source=%s host=%s error_type=%s",
                self.name,
                urlsplit(url).hostname,
                type(exc).__name__,
            )
            raise SourceCollectionError(f"{self.name} 采集失败：{exc}") from exc

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": "news-agent/0.1 (+local content operations demo)"}
        if extra:
            headers.update(extra)
        return headers

    async def fetch_text_limited(
        self, url: str, max_bytes: int, headers: dict[str, str] | None = None
    ) -> str:
        """读取受大小限制的公开响应，避免把异常大页面放入数据库或提示词。"""
        logger.info(
            "source_http_limited_started source=%s host=%s max_bytes=%s",
            self.name,
            urlsplit(url).hostname,
            max_bytes,
        )
        try:
            async with self.client.stream(
                "GET",
                url,
                headers=self._headers(headers),
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise SourceCollectionError(f"{self.name} 正文超过 {max_bytes} 字节上限")
                    chunks.append(chunk)
                text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                logger.info(
                    "source_http_limited_finished source=%s host=%s bytes=%s",
                    self.name,
                    urlsplit(url).hostname,
                    size,
                )
                return text
        except SourceCollectionError:
            logger.warning(
                "source_http_limited_rejected source=%s host=%s",
                self.name,
                urlsplit(url).hostname,
            )
            raise
        except httpx.HTTPError as exc:
            logger.warning(
                "source_http_limited_failed source=%s host=%s error_type=%s",
                self.name,
                urlsplit(url).hostname,
                type(exc).__name__,
            )
            raise SourceCollectionError(f"{self.name} 采集失败：{exc}") from exc

    @abstractmethod
    async def collect(self, limit: int) -> list[RawSourceItem]:
        raise NotImplementedError
