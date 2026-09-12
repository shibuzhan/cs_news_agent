from pathlib import Path
import base64

import httpx
import pytest

from app.sources.arxiv import ArxivCollector
from app.sources.github import GitHubTrendingCollector
from app.sources.hackernews import HackerNewsCollector
from app.sources.rss import RssCollector

FIXTURES = Path(__file__).parent / "fixtures"


def test_arxiv_parser_normalizes_atom_entry():
    items = ArxivCollector.parse_feed((FIXTURES / "arxiv.xml").read_text(encoding="utf-8"), 10)
    assert len(items) == 1
    assert items[0].external_id == "2609.00001v1"
    assert items[0].metadata["categories"] == ["cs.AI"]


def test_github_parser_reads_trending_metrics():
    items = GitHubTrendingCollector.parse_page(
        (FIXTURES / "github.html").read_text(encoding="utf-8"), "daily", 10
    )
    assert items[0].external_id == "example/hot-agent"
    assert items[0].metrics["stars_period"] == 321
    assert items[0].metrics["stars_total"] == 12345
    assert items[0].metadata["rank"] == 1


@pytest.mark.asyncio
async def test_github_collector_merges_daily_and_weekly_rank():
    html = (FIXTURES / "github.html").read_text(encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await GitHubTrendingCollector(client).collect(10)
    assert len(items) == 1
    assert items[0].metadata["periods"] == {"daily": 1, "weekly": 1}
    assert items[0].metadata["period_metrics"] == {
        "daily": {"rank": 1, "stars_period": 321},
        "weekly": {"rank": 1, "stars_period": 321},
    }


@pytest.mark.asyncio
async def test_github_readme_enrichment_keeps_full_readme_for_selected_project():
    html = (FIXTURES / "github.html").read_text(encoding="utf-8")
    readme = "# Hot Agent\n\n" + ("Useful project documentation.\n" * 200)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "name": "README.md",
                    "encoding": "base64",
                    "content": base64.b64encode(readme.encode()).decode(),
                    "download_url": "https://raw.githubusercontent.com/example/hot-agent/main/README.md",
                    "git_url": "https://api.github.com/repos/example/hot-agent/git/blobs/very-long-id",
                },
            )
        return httpx.Response(200, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = GitHubTrendingCollector(client)
        selected = (await collector.collect(1))[:1]
        enriched = await collector.enrich_items(selected)
    assert enriched[0].content == readme
    assert "api.github.com" not in enriched[0].content
    assert "download_url" not in enriched[0].content
    assert enriched[0].metadata["readme_fetch_status"] == "success"
    assert enriched[0].metadata["content_origin"] == "github_readme"


def test_rss_parser_preserves_source_link():
    items = RssCollector.parse_feed(
        (FIXTURES / "rss.xml").read_text(encoding="utf-8"),
        "https://example.com/feed.xml",
        10,
    )
    assert items[0].source_name == "Example Official Blog"
    assert str(items[0].url) == "https://example.com/releases/2"


@pytest.mark.asyncio
async def test_hacker_news_fetches_external_article_body():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("topstories.json"):
            return httpx.Response(200, json=[101])
        if request.url.host == "example.com":
            return httpx.Response(
                200,
                text="<html><body><article><h1>AI tool</h1><p>First complete paragraph.</p><p>Second complete paragraph.</p></article></body></html>",
            )
        return httpx.Response(
            200,
            json={
                "id": 101,
                "type": "story",
                "title": "A new AI tool",
                "url": "https://example.com/ai-tool",
                "by": "editor",
                "time": 1788264000,
                "score": 120,
                "descendants": 30,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await HackerNewsCollector(client).collect(1)
    assert items[0].external_id == "101"
    assert items[0].metrics == {"score": 120, "comments": 30}
    assert "First complete paragraph." in items[0].content
    assert items[0].metadata["article_fetch_status"] == "success"
