from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from app.config import Settings
from app.domain.models import RawSourceItem, SourceKind
from app.services.collection import CollectionBatch
from app.sources.base import SourceCollector
from app.sources.registry import build_collectors
from app.tools.contracts import CollectionToolRequest, CollectionToolResult


logger = logging.getLogger("news_agent.source_tool")


class ToolValidationError(ValueError):
    pass


class SourceCollectionTool:
    """对一个固定来源的最小权限采集包装。"""

    def __init__(self, source_kind: SourceKind, collector: SourceCollector):
        self.source_kind = source_kind
        self.collector = collector

    async def invoke(self, request: CollectionToolRequest) -> CollectionBatch:
        if request.source != self.source_kind:
            raise ToolValidationError(
                f"{self.source_kind.value} Tool 不接受 {request.source.value} 请求"
            )
        started_at = datetime.now(UTC)
        target = (request.target or "").strip() or None
        fetch_project = getattr(self.collector, "fetch_project", None)
        logger.info(
            "source_tool_started source=%s limit=%s mode=%s target=%s",
            self.source_kind.value,
            request.limit,
            "project" if target else "list",
            target or "-",
        )
        try:
            if target and callable(fetch_project):
                # 点名项目：只抓这一个仓库，不读榜单、不参与热度排序。
                item = await fetch_project(target)
                logger.info(
                    "source_tool_finished source=%s mode=project target=%s readme=%s",
                    self.source_kind.value,
                    target,
                    item.metadata.get("readme_fetch_status"),
                )
                return CollectionBatch(
                    source_kind=self.source_kind.value,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    items=[item],
                    selected_items=[item],
                    selection_metadata={"mode": "project", "target": target},
                )
            items = await self.collector.collect(request.limit)
            logger.info(
                "source_tool_finished source=%s received=%s",
                self.source_kind.value,
                len(items),
            )
            return CollectionBatch(
                source_kind=self.source_kind.value,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                items=items,
            )
        except Exception as exc:  # 工具失败交由主 Agent 汇总，不扩散为整次失败
            logger.warning(
                "source_tool_failed source=%s mode=%s error_type=%s",
                self.source_kind.value,
                "project" if target else "list",
                type(exc).__name__,
            )
            return CollectionBatch(
                source_kind=self.source_kind.value,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                items=[],
                error=str(exc),
            )

    async def enrich_items(self, items: list[RawSourceItem]) -> list[RawSourceItem]:
        """仅调用采集器显式实现的富化能力，主 Agent 决定候选和调用时机。"""
        enrich = getattr(self.collector, "enrich_items", None)
        if not callable(enrich):
            return items
        logger.info(
            "source_tool_enrichment_started source=%s item_count=%s",
            self.source_kind.value,
            len(items),
        )
        enriched = await enrich(items)
        logger.info(
            "source_tool_enrichment_finished source=%s item_count=%s",
            self.source_kind.value,
            len(enriched),
        )
        return enriched

    @staticmethod
    def result_from_batch(batch: CollectionBatch) -> CollectionToolResult:
        return CollectionToolResult(
            source=SourceKind(batch.source_kind),
            status="failed" if batch.error else "collected",
            item_count=len(batch.items),
            started_at=batch.started_at,
            finished_at=batch.finished_at,
            error=batch.error,
        )


def build_source_tools(
    client: httpx.AsyncClient, settings: Settings
) -> dict[SourceKind, SourceCollectionTool]:
    collectors = build_collectors(client, settings)
    tools = {
        SourceKind(source_kind): SourceCollectionTool(
            SourceKind(source_kind), collector
        )
        for source_kind, collector in collectors.items()
    }
    if not settings.rss_feed_list:
        tools.pop(SourceKind.RSS, None)
    return tools
