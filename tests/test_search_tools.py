from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.tools.search_tools import ExaMcpSearchTool


@pytest.mark.asyncio
async def test_exa_search_limits_queries_and_result_count_before_mcp_call() -> None:
    tool = ExaMcpSearchTool(
        SimpleNamespace(
            exa_mcp_enabled=True,
            exa_search_max_queries=9,
            exa_search_max_results=50,
            exa_search_result_max_chars=500,
        )
    )
    calls: list[tuple[str, dict]] = []

    async def fake_call(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return "来源：https://example.com/result"

    tool._call = fake_call  # type: ignore[method-assign]
    evidence = await tool.search(["RCE 是什么", "RCE 是什么", "Chromium sandbox", "third query"])

    assert len(calls) == 2
    assert all(name == "web_search_exa" for name, _ in calls)
    assert all(arguments["numResults"] == 5 for _, arguments in calls)
    assert [entry["id"] for entry in evidence] == ["exa-search-1", "exa-search-2"]
    assert evidence[0]["url"] == "https://example.com/result"


@pytest.mark.asyncio
async def test_disabled_exa_tool_never_calls_remote_mcp() -> None:
    tool = ExaMcpSearchTool(
        SimpleNamespace(
            exa_mcp_enabled=False,
            exa_search_max_queries=2,
            exa_search_max_results=5,
            exa_search_result_max_chars=500,
        )
    )

    assert await tool.search(["should not search"]) == []
