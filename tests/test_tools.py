from __future__ import annotations

import pytest

from app.domain.models import SourceKind
from app.tools.contracts import CollectionToolRequest
from app.tools.source_tools import SourceCollectionTool


class EmptyCollector:
    async def collect(self, limit: int):
        return []


class BrokenCollector:
    async def collect(self, limit: int):
        raise RuntimeError("source unavailable")


@pytest.mark.asyncio
async def test_source_tool_returns_a_successful_batch_for_its_fixed_source():
    tool = SourceCollectionTool(SourceKind.ARXIV, EmptyCollector())

    batch = await tool.invoke(CollectionToolRequest(source=SourceKind.ARXIV, limit=3))

    assert batch.source_kind == "arxiv"
    assert batch.items == []
    assert batch.error is None
    assert tool.result_from_batch(batch).status == "collected"


@pytest.mark.asyncio
async def test_source_tool_captures_its_own_failure():
    tool = SourceCollectionTool(SourceKind.HACKER_NEWS, BrokenCollector())

    batch = await tool.invoke(
        CollectionToolRequest(source=SourceKind.HACKER_NEWS, limit=3)
    )

    assert batch.error == "source unavailable"
    assert tool.result_from_batch(batch).status == "failed"
