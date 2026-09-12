from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.models import SourceKind


class CollectionToolRequest(BaseModel):
    """每个采集 Tool 只接受已登记来源和受限条数。"""

    source: SourceKind
    limit: int = Field(ge=1, le=50)


class CollectionToolResult(BaseModel):
    source: SourceKind
    status: str
    item_count: int
    started_at: datetime
    finished_at: datetime
    error: str | None = None
