from __future__ import annotations

from datetime import UTC, datetime
from math import log1p

from app.domain.models import NormalizedItem, SourceKind


def calculate_hot_score(item: NormalizedItem, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    age_hours = 24.0
    if item.published_at:
        published = item.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age_hours = max((now - published).total_seconds() / 3600, 0)
    freshness = 100 / (1 + age_hours / 24)

    if item.source_kind == SourceKind.GITHUB:
        signal = float(item.metrics.get("stars_period", 0)) * 2 + log1p(
            float(item.metrics.get("stars_total", 0))
        ) * 5
    elif item.source_kind == SourceKind.HACKER_NEWS:
        signal = float(item.metrics.get("score", 0)) + float(
            item.metrics.get("comments", 0)
        ) * 0.5
    elif item.source_kind == SourceKind.ARXIV:
        signal = 35
    else:
        signal = 20
    return round(freshness + signal, 3)
