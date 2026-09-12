"""Pydantic HttpUrl 与 ORM 行的兼容回归。

真实故障：草稿的 `source_url` 是 `HttpUrl` 对象，直接传给做 `url.strip()` 的检索工具或
httpx 的 JSON 编码会抛 `AttributeError`，表现为“对话模型暂时不可用”（attribution 完全误导）。
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import HttpUrl


@pytest.mark.asyncio
async def test_search_tool_accepts_http_url_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exa 检索工具的入参可能是 HttpUrl，必须先归一化为 str。"""
    from app.tools.search_tools import ExaMcpSearchTool

    tool = ExaMcpSearchTool(
        type(
            "S",
            (),
            {"exa_mcp_enabled": True, "exa_search_max_results": 2, "exa_search_result_max_chars": 1000},
        )()
    )
    captured: dict = {}

    async def fake_call(name: str, arguments: dict) -> str:
        captured["arguments"] = arguments
        return "https://example.com/page 正文"

    monkeypatch.setattr(tool, "_call", fake_call)

    evidence = await tool.fetch([HttpUrl("https://example.com/a")])

    assert captured["arguments"]["urls"] == ["https://example.com/a"]
    assert evidence and evidence[0]["id"] == "exa-fetch-1"


def test_source_media_tools_coerce_draft_source_url() -> None:
    from app.agent_tools import source_media_tools

    source = inspect.getsource(source_media_tools)

    assert 'str(draft.source_url or "")' in source
    # 快照存储要求 RawSourceItem，不能直接传 ORM 行。
    assert "RawSourceItem(" in source
    assert "restore_github_readme(draft.id, item)" in source


def test_delivery_coerces_source_url_for_json_payload() -> None:
    from app.services import auto_delivery

    source = inspect.getsource(auto_delivery)

    assert 'source_url=str(draft.source_url or "")' in source


def test_deep_agent_failure_logs_traceback() -> None:
    """只记录 error_type 会让这类问题极难定位，必须保留堆栈。"""
    from app.agents import content_deep_agent

    source = inspect.getsource(content_deep_agent)

    assert "exc_info=cause" in source
