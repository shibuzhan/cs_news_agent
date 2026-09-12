from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.models import RawSourceItem, SourceKind
from app.services.collection import collect_isolated
from app.services.generator import DeterministicDraftGenerator, build_generator
from app.config import Settings


class SuccessfulCollector:
    async def collect(self, limit: int) -> list[RawSourceItem]:
        return [
            RawSourceItem(
                source_kind=SourceKind.RSS,
                external_id="release-1",
                title="A useful product release",
                url="https://example.com/releases/1",
                published_at=datetime.now(UTC),
                summary="A release with enough concrete details for a draft.",
                source_name="Example Official Blog",
            )
        ][:limit]


class FailingCollector:
    async def collect(self, limit: int) -> list[RawSourceItem]:
        raise RuntimeError("upstream unavailable")


@pytest.mark.asyncio
async def test_collection_failure_is_isolated_per_source():
    batches = await collect_isolated(
        {
            "rss": SuccessfulCollector(),
            "github": FailingCollector(),
        },
        limit=1,
    )

    assert [batch.source_kind for batch in batches] == ["rss", "github"]
    assert len(batches[0].items) == 1
    assert batches[0].error is None
    assert batches[1].items == []
    assert batches[1].error == "upstream unavailable"
    assert batches[0].started_at <= batches[0].finished_at


def test_llm_requires_explicit_enable_even_when_credentials_exist():
    settings = Settings(
        llm_enabled=False,
        openai_api_key="configured-but-not-used",
        llm_model="example-model",
    )

    assert isinstance(build_generator(settings), DeterministicDraftGenerator)
