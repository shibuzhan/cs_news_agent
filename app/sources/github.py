from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from app.domain.models import RawSourceItem, SourceKind
from app.sources.base import SourceCollectionError, SourceCollector


def _number(text: str) -> int:
    cleaned = text.strip().replace(",", "")
    match = re.search(r"\d+", cleaned)
    return int(match.group()) if match else 0


def decode_github_readme_response(payload: str) -> str:
    """只提取 GitHub README API 的 Markdown 正文，拒绝 API URL 与元数据进入证据包。"""
    try:
        response = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SourceCollectionError("GitHub README 响应不是预期 JSON") from exc
    if not isinstance(response, dict) or response.get("encoding") != "base64":
        raise SourceCollectionError("GitHub README 响应缺少受支持的 Base64 正文")
    encoded_content = response.get("content")
    if not isinstance(encoded_content, str) or not encoded_content.strip():
        raise SourceCollectionError("GitHub README 响应没有正文内容")
    try:
        return base64.b64decode(encoded_content, validate=False).decode("utf-8", errors="replace")
    except (ValueError, binascii.Error) as exc:
        raise SourceCollectionError("GitHub README 正文无法解码") from exc


class GitHubTrendingCollector(SourceCollector):
    name = "GitHub Trending"
    endpoint = "https://github.com/trending"
    periods = ("daily", "weekly")

    def __init__(
        self, client, response_max_bytes: int = 1_000_000, content_max_chars: int = 500_000
    ):
        super().__init__(client)
        self.response_max_bytes = response_max_bytes
        self.content_max_chars = content_max_chars

    @staticmethod
    def parse_page(html: str, period: str, limit: int) -> list[RawSourceItem]:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("article.Box-row")
        if not rows:
            raise SourceCollectionError("GitHub Trending 页面结构无法识别")
        items: list[RawSourceItem] = []
        for rank, row in enumerate(rows[:limit], start=1):
            repo_link = row.select_one("h2 a")
            if repo_link is None or not repo_link.get("href"):
                continue
            full_name = repo_link.get_text(" ", strip=True).replace(" ", "")
            url = f"https://github.com{repo_link['href']}"
            description = row.select_one("p")
            language = row.select_one('[itemprop="programmingLanguage"]')
            star_link = row.select_one('a[href$="/stargazers"]')
            fork_link = row.select_one('a[href$="/forks"]')
            period_text = row.select_one("span.d-inline-block.float-sm-right")
            stars_period = _number(period_text.get_text(" ", strip=True)) if period_text else 0
            items.append(
                RawSourceItem(
                    source_kind=SourceKind.GITHUB,
                    external_id=full_name,
                    title=full_name,
                    url=url,
                    author=full_name.split("/", 1)[0] if "/" in full_name else None,
                    published_at=datetime.now(UTC),
                    summary=description.get_text(" ", strip=True) if description else "",
                    content=description.get_text(" ", strip=True) if description else "",
                    source_name="GitHub Trending",
                    metrics={
                        "stars_total": _number(star_link.get_text(" ", strip=True)) if star_link else 0,
                        "forks": _number(fork_link.get_text(" ", strip=True)) if fork_link else 0,
                        "stars_period": stars_period,
                    },
                    metadata={"period": period, "rank": rank, "language": language.get_text(strip=True) if language else None},
                )
            )
        return items

    async def collect(self, limit: int) -> list[RawSourceItem]:
        merged: dict[str, RawSourceItem] = {}
        for period in self.periods:
            html = await self.fetch_text(f"{self.endpoint}?since={period}")
            for item in self.parse_page(html, period, limit):
                current = merged.get(item.external_id)
                if current is None:
                    rank = item.metadata.pop("rank")
                    item.metadata["periods"] = {period: rank}
                    item.metadata["period_metrics"] = {
                        period: {
                            "rank": rank,
                            "stars_period": int(item.metrics.get("stars_period", 0)),
                        }
                    }
                    merged[item.external_id] = item
                    continue
                current.metadata.setdefault("periods", {})[period] = item.metadata["rank"]
                current.metadata.setdefault("period_metrics", {})[period] = {
                    "rank": item.metadata["rank"],
                    "stars_period": int(item.metrics.get("stars_period", 0)),
                }
                current.metrics["stars_period"] = max(
                    int(current.metrics.get("stars_period", 0)),
                    int(item.metrics.get("stars_period", 0)),
                )
        return list(merged.values())

    async def enrich_items(self, items: list[RawSourceItem]) -> list[RawSourceItem]:
        """仅为主 Agent 已选中的单个项目读取 README，不能用于批量抓取。"""
        enriched: list[RawSourceItem] = []
        for item in items:
            metadata: dict[str, Any] = dict(item.metadata)
            try:
                readme_response = await self.fetch_text_limited(
                    f"https://api.github.com/repos/{item.external_id}/readme",
                    self.response_max_bytes,
                )
                readme = decode_github_readme_response(readme_response)
                metadata.update({"content_origin": "github_readme", "readme_fetch_status": "success"})
                enriched.append(
                    item.model_copy(
                        update={
                            "content": readme[: self.content_max_chars],
                            "metadata": metadata,
                        }
                    )
                )
            except SourceCollectionError as exc:
                metadata.update({"content_origin": "trending_description", "readme_fetch_status": "failed", "readme_fetch_error": str(exc)})
                enriched.append(item.model_copy(update={"metadata": metadata}))
        return enriched
