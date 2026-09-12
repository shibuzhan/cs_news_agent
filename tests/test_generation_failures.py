from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

# 仅装配模型故障分支，不连接项目 PostgreSQL 或模型服务。
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.domain.models import ConversationRunStatus
from app.services.generator import GenerationError, ResilientDraftGenerator
from app.worker import collection_completion


def test_provider_timeout_is_reported_instead_of_generating_a_fallback_draft() -> None:
    class TimeoutPrimary:
        def generate(self, _item):
            raise TimeoutError("upstream did not respond")

    item = SimpleNamespace(
        source_kind=SimpleNamespace(value="github"),
        external_id="owner/repository",
    )

    with pytest.raises(GenerationError, match="内容模型请求超时"):
        ResilientDraftGenerator(TimeoutPrimary()).generate(item)


def test_collection_failure_keeps_the_safe_model_error_message() -> None:
    result = SimpleNamespace(
        status=SimpleNamespace(value="failed"),
        source_errors=[],
        created=0,
        runs=[{"errors": [{"error": "内容模型请求超时，未在等待时限内收到响应"}]}],
    )

    status, summary, event_status = collection_completion(result)

    assert status == ConversationRunStatus.FAILED
    assert event_status == "failed"
    assert "内容模型请求超时" in summary
