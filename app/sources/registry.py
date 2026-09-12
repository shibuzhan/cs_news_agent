from __future__ import annotations

import httpx

from app.config import Settings
from app.sources import ArxivCollector, GitHubTrendingCollector, HackerNewsCollector, RssCollector
from app.sources.base import SourceCollector


def build_collectors(client: httpx.AsyncClient, settings: Settings) -> dict[str, SourceCollector]:
    return {
        "arxiv": ArxivCollector(client, settings.arxiv_category_list),
        "github": GitHubTrendingCollector(
            client,
            settings.source_response_max_bytes,
            settings.source_content_max_chars,
            token=settings.github_token,
        ),
        "hacker_news": HackerNewsCollector(
            client, settings.source_response_max_bytes, settings.source_content_max_chars
        ),
        "rss": RssCollector(client, settings.rss_feed_list),
    }
