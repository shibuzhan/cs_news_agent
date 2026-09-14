"""按来源类型重新联网抓取正文：重写任务的兜底与“刷新来源”工具共用同一口径。

单独成模块是为了避免 `app.agent_tools.draft_actions` 反向导入 `app.worker`
（worker 在导入时会构造 Settings 并配置可观测性，工具层不该承担这些副作用）。
"""

from __future__ import annotations

import httpx

from app.domain.models import RawSourceItem
from app.tools.source_tools import build_source_tools

# 与 GitHub 采集器的最小 README 长度一致：更短的“正文”只是 Trending 简介。
MIN_SOURCE_CONTENT_CHARS = 200
READMELESS_SOURCE_HINT = "本轮没有取到正文（README 缺失或接口失败），已保留原草稿。"


async def fetch_live_source(
    settings, item: RawSourceItem, *, timeout_seconds: float | None = None,
) -> RawSourceItem:
    """调用来源采集器的富化能力重新抓一次正文（抓不到就抛错，绝不返回空正文）。

    采集器没有实现富化时 `enrich_items` 会原样返回入参：这时不能当成“抓取成功”，
    否则刷新来源会静默地什么也没做，用户以为来源已更新。
    """
    timeout = httpx.Timeout(timeout_seconds or settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        tools = build_source_tools(client, settings)
        tool = tools.get(item.source_kind)
        if tool is None:
            raise ValueError(f"当前草稿来源 {item.source_kind.value} 不支持重新获取")
        refreshed = await tool.enrich_items([item])
    if not refreshed or refreshed[0] is item:
        raise ValueError(f"{item.source_kind.value} 来源不支持重新获取正文")
    updated = refreshed[0]
    if len((updated.content or "").strip()) < MIN_SOURCE_CONTENT_CHARS:
        raise ValueError(READMELESS_SOURCE_HINT)
    return updated
