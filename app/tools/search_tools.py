"""受控 Exa 远程 MCP 搜索 Tool。

模型只可提出查询词；本模块固定远程 MCP 地址、工具名、调用次数和结果长度，
并将返回结果整理为可写入证据包的普通字典。
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from app.config import Settings


logger = logging.getLogger("news_agent.exa_search_tool")
_URL_RE = re.compile(r"https?://[^\s<>\]})]+")
_ALLOWED_TOOLS = {"web_search_exa", "web_fetch_exa"}


class ExaMcpSearchError(RuntimeError):
    """对上层隐藏远程 MCP 的协议与认证细节。"""


class ExaMcpSearchTool:
    """仅允许 Exa 搜索和已知 URL 正文提取的最小权限包装。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.exa_mcp_enabled

    async def search(self, queries: list[str]) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        cleaned = self._clean_queries(queries)
        evidence: list[dict[str, Any]] = []
        for index, query in enumerate(cleaned, start=1):
            text = await self._call(
                "web_search_exa",
                {"query": query, "numResults": self._max_results},
            )
            evidence.append(self._as_evidence(f"exa-search-{index}", query, text))
        return evidence

    async def fetch(self, urls: list[str]) -> list[dict[str, Any]]:
        """仅供后续明确的页面补全文本能力调用，不由当前生成流程自动触发。"""
        if not self.enabled:
            return []
        # 入参可能来自 Pydantic HttpUrl（草稿字段）而不是 str，必须先归一化再处理。
        cleaned = [
            text
            for raw in urls
            if (text := str(raw or "").strip()).startswith(("https://", "http://"))
        ]
        cleaned = cleaned[: self._max_results]
        if not cleaned:
            return []
        text = await self._call("web_fetch_exa", {"urls": cleaned})
        return [
            self._as_evidence(f"exa-fetch-{index}", url, text)
            for index, url in enumerate(cleaned, start=1)
        ]

    @property
    def _max_queries(self) -> int:
        return min(max(self.settings.exa_search_max_queries, 1), 2)

    @property
    def _max_results(self) -> int:
        return min(max(self.settings.exa_search_max_results, 1), 5)

    def _clean_queries(self, queries: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for query in queries:
            value = " ".join(str(query).split()).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            cleaned.append(value[:300])
            if len(cleaned) == self._max_queries:
                break
        return cleaned

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name not in _ALLOWED_TOOLS:
            raise ExaMcpSearchError("不允许调用未登记的 Exa MCP Tool")
        try:
            # MCP Python SDK 负责 Streamable HTTP 会话、初始化和会话关闭；
            # 不手工拼接 JSON-RPC，避免将 Exa 接入退化为任意 HTTP 请求。
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client

            headers = {"x-api-key": self.settings.exa_api_key} if self.settings.exa_api_key else {}
            timeout = httpx2.Timeout(
                self.settings.request_timeout_seconds,
                read=max(self.settings.request_timeout_seconds, 60),
            )
            async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client:
                transport = streamable_http_client(self.settings.exa_mcp_url, http_client=http_client)
                async with Client(transport) as client:
                    result = await client.call_tool(tool_name, arguments)
        except Exception as exc:
            logger.warning(
                "exa_mcp_call_failed tool=%s error_type=%s",
                tool_name,
                type(exc).__name__,
            )
            raise ExaMcpSearchError("联网搜索暂不可用，已跳过外部补充证据") from exc

        if result.is_error:
            logger.warning("exa_mcp_tool_error tool=%s", tool_name)
            raise ExaMcpSearchError("联网搜索返回失败，已跳过外部补充证据")
        parts = [getattr(block, "text", "") for block in result.content]
        text = "\n".join(part for part in parts if isinstance(part, str)).strip()
        if not text:
            raise ExaMcpSearchError("联网搜索未返回可用文本")
        logger.info("exa_mcp_call_finished tool=%s text_length=%s", tool_name, len(text))
        return text[: self.settings.exa_search_result_max_chars]

    @staticmethod
    def _as_evidence(evidence_id: str, query: str, text: str) -> dict[str, Any]:
        urls = _URL_RE.findall(text)
        return {
            "id": evidence_id,
            "title": f"Exa 联网检索：{query}",
            "url": urls[0] if urls else "https://exa.ai",
            "summary": text[:1200],
            "content": text,
            "source_name": "Exa MCP",
            "search_query": query,
            "retrieved_at": datetime.now(UTC).isoformat(),
        }
