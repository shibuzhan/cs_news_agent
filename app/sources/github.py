from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from app.domain.models import ContentCategory, RawSourceItem, SourceKind
from app.sources.base import SourceCollectionError, SourceCollector


logger = logging.getLogger("news_agent.github")

# README 重取间隔基数（秒）：第 1 次失败后等 2 秒，第 2 次失败后等 4 秒。
README_RETRY_DELAY_SECONDS = 2.0
# README 正文少于此长度时不认为拿到了可用来源正文。
MIN_README_CHARS = 200


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
        self,
        client,
        response_max_bytes: int = 1_000_000,
        content_max_chars: int = 500_000,
        token: str | None = None,
        readme_attempts: int = 3,
    ):
        super().__init__(client)
        self.response_max_bytes = response_max_bytes
        self.content_max_chars = content_max_chars
        # 未配置令牌时 GitHub API 只有每小时 60 次的匿名配额，README 很容易被限流。
        self.token = token
        self.readme_attempts = max(readme_attempts, 1)

    def _api_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _readme_failure_kind(exc: Exception) -> str:
        """只分类，不回显 GitHub 原始响应。"""
        text = str(exc).casefold()
        if "403" in text or "429" in text or "rate limit" in text:
            return "rate_limited"
        if "404" in text:
            return "not_found"
        return "unavailable"

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

    async def fetch_project(self, external_id: str) -> RawSourceItem:
        """按用户点名的 owner/repo 抓取仓库元数据与 README，不读 Trending 榜单。

        README 仍按受控次数重试：拿不到就如实标记 `readme_fetch_status=failed`，
        由流水线拒绝生成，而不是拿描述凑一篇文案。
        """
        metadata: dict[str, Any] = {"project_target": True}
        title = external_id
        url = f"https://github.com/{external_id}"
        summary = ""
        metrics: dict[str, Any] = {}
        try:
            payload = await self.fetch_text_limited(
                f"https://api.github.com/repos/{external_id}",
                self.response_max_bytes,
                self._api_headers(),
            )
            repo = json.loads(payload)
            if isinstance(repo, dict):
                title = str(repo.get("full_name") or external_id)
                url = str(repo.get("html_url") or url)
                summary = str(repo.get("description") or "")[:5000]
                license_info = repo.get("license") if isinstance(repo.get("license"), dict) else {}
                metrics = {
                    "stars_total": int(repo.get("stargazers_count") or 0),
                    "forks": int(repo.get("forks_count") or 0),
                }
                metadata.update(
                    {
                        "project_language": repo.get("language"),
                        "project_topics": repo.get("topics") if isinstance(repo.get("topics"), list) else [],
                        "project_homepage": repo.get("homepage"),
                        "project_pushed_at": repo.get("pushed_at"),
                        "project_created_at": repo.get("created_at"),
                        "project_license": license_info.get("spdx_id") or license_info.get("name"),
                        "project_open_issues": repo.get("open_issues_count"),
                    }
                )
        except Exception as exc:  # 元数据失败不阻断：README 才是生成所需的本体
            logger.warning(
                "github_project_metadata_failed project=%s error_type=%s",
                external_id,
                type(exc).__name__,
            )
            metadata["project_metadata_status"] = "failed"
        content, readme_metadata = await self._fetch_readme(external_id)
        metadata.update(readme_metadata)
        return RawSourceItem(
            source_kind=SourceKind.GITHUB,
            external_id=external_id,
            title=title,
            url=url,
            source_name="GitHub 项目",
            summary=summary,
            content=content,
            metrics=metrics,
            category=ContentCategory.OPEN_SOURCE,
            metadata=metadata,
        )

    async def _fetch_readme(self, external_id: str) -> tuple[str, dict[str, Any]]:
        """读取 README 并按受控次数重试；返回正文与需要并入的元数据。"""
        last_error: Exception | None = None
        for attempt in range(1, self.readme_attempts + 1):
            try:
                response = await self.fetch_text_limited(
                    f"https://api.github.com/repos/{external_id}/readme",
                    self.response_max_bytes,
                    self._api_headers(),
                )
                readme = decode_github_readme_response(response)
                return readme[: self.content_max_chars], {
                    "content_origin": "github_readme",
                    "readme_fetch_status": "success",
                    "readme_attempts": attempt,
                }
            except SourceCollectionError as exc:
                last_error = exc
                logger.warning(
                    "github_readme_fetch_failed project=%s attempt=%s kind=%s",
                    external_id,
                    attempt,
                    self._readme_failure_kind(exc),
                )
                if attempt < self.readme_attempts:
                    await asyncio.sleep(README_RETRY_DELAY_SECONDS * attempt)
        return "", {
            "content_origin": "trending_description",
            "readme_fetch_status": "failed",
            "readme_fetch_error": self._readme_failure_kind(last_error or Exception()),
            "readme_attempts": self.readme_attempts,
        }

    async def enrich_items(self, items: list[RawSourceItem]) -> list[RawSourceItem]:
        """仅为主 Agent 已选中的单个项目读取 README，不能用于批量抓取。

        生成前必须拿到 README：失败时按受控次数重试，仍失败则如实标记，由流水线拒绝生成。
        """
        enriched: list[RawSourceItem] = []
        for item in items:
            metadata: dict[str, Any] = dict(item.metadata)
            last_error: Exception | None = None
            for attempt in range(1, self.readme_attempts + 1):
                try:
                    readme_response = await self.fetch_text_limited(
                        f"https://api.github.com/repos/{item.external_id}/readme",
                        self.response_max_bytes,
                        self._api_headers(),
                    )
                    readme = decode_github_readme_response(readme_response)
                    metadata.update(
                        {
                            "content_origin": "github_readme",
                            "readme_fetch_status": "success",
                            "readme_attempts": attempt,
                        }
                    )
                    metadata.pop("readme_fetch_error", None)
                    enriched.append(
                        item.model_copy(
                            update={
                                "content": readme[: self.content_max_chars],
                                "metadata": metadata,
                            }
                        )
                    )
                    break
                except SourceCollectionError as exc:
                    last_error = exc
                    logger.warning(
                        "github_readme_fetch_failed project=%s attempt=%s kind=%s",
                        item.external_id,
                        attempt,
                        self._readme_failure_kind(exc),
                    )
                    if attempt < self.readme_attempts:
                        await asyncio.sleep(README_RETRY_DELAY_SECONDS * attempt)
            else:
                metadata.update(
                    {
                        "content_origin": "trending_description",
                        "readme_fetch_status": "failed",
                        "readme_fetch_error": self._readme_failure_kind(last_error or Exception()),
                        "readme_attempts": self.readme_attempts,
                    }
                )
                enriched.append(item.model_copy(update={"metadata": metadata}))
        return enriched
