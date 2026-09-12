from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.domain.models import NormalizedItem, RawSourceItem
from app.services.generator import DraftGenerator
from app.services.normalizer import normalize_item
from app.sources.base import SourceCollector
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository
from app.workflows.content_workflow import ContentPipeline


logger = logging.getLogger("news_agent.collection")


@dataclass(frozen=True)
class CollectionBatch:
    source_kind: str
    started_at: datetime
    finished_at: datetime
    items: list[RawSourceItem]
    error: str | None = None
    selected_items: list[RawSourceItem] | None = None
    selection_metadata: dict[str, Any] = field(default_factory=dict)


async def collect_isolated(
    collectors: dict[str, SourceCollector], limit: int
) -> list[CollectionBatch]:
    batches: list[CollectionBatch] = []
    for source_kind, collector in collectors.items():
        started_at = datetime.now(UTC)
        logger.info("source_collection_started source=%s limit=%s", source_kind, limit)
        try:
            items = await collector.collect(limit)
            logger.info("source_collection_finished source=%s received=%s", source_kind, len(items))
            batches.append(
                CollectionBatch(
                    source_kind=source_kind,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    items=items,
                )
            )
        except Exception as exc:  # 来源隔离：一个失败不能中断其他来源
            logger.warning(
                "source_collection_failed source=%s error_type=%s",
                source_kind,
                type(exc).__name__,
            )
            batches.append(
                CollectionBatch(
                    source_kind=source_kind,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    items=[],
                    error=str(exc),
                )
            )
    return batches


def persist_collection_batch(
    batch: CollectionBatch,
    generator: DraftGenerator,
    agent_run_id: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    logger.info(
        "collection_batch_persist_started source=%s item_count=%s has_error=%s",
        batch.source_kind,
        len(batch.items),
        bool(batch.error),
    )
    with SessionLocal() as session:
        repository = ContentRepository(session)
        if batch.error:
            run = repository.create_collection_run(
                source_kind=batch.source_kind,
                status="failed",
                started_at=batch.started_at,
                finished_at=batch.finished_at,
                error_message=batch.error,
                agent_run_id=agent_run_id,
            )
            session.commit()
            logger.warning("collection_batch_persist_failed source=%s", batch.source_kind)
            return {"run_id": run.id, "source": batch.source_kind, "status": "failed", "error": batch.error}

        pipeline = ContentPipeline(session, generator, settings)
        if batch.source_kind == "github":
            pipeline.save_github_candidates(batch.items)
            result = pipeline.process_github_single(
                batch.selected_items or [], candidate_count=len(batch.items)
            )
        else:
            result = pipeline.process(batch.items)
        status = "partial" if result["errors"] else "success"
        error_message = None
        if result["errors"]:
            error_message = "; ".join(error["error"] for error in result["errors"][:5])
        run = repository.create_collection_run(
            source_kind=batch.source_kind,
            status=status,
            started_at=batch.started_at,
            finished_at=batch.finished_at,
            item_count=len(batch.items),
            created_count=result["created"],
            duplicate_count=result["duplicates"],
            skipped_count=result["skipped"],
            error_message=error_message,
            agent_run_id=agent_run_id,
        )
        normalized_items: list[NormalizedItem] = []
        for raw in batch.items:
            normalized = normalize_item(raw)
            normalized_items.append(normalized)
        snapshot_count = repository.save_trending_snapshots(
            run.id, normalized_items, batch.finished_at
        )
        session.commit()
        logger.info(
            "collection_batch_persist_finished source=%s created=%s duplicates=%s skipped=%s",
            batch.source_kind,
            result["created"],
            result["duplicates"],
            result["skipped"],
        )
        return {
            "run_id": run.id,
            "source": batch.source_kind,
            "status": status,
            "snapshot_count": snapshot_count,
            "selection": batch.selection_metadata,
            **result,
        }
