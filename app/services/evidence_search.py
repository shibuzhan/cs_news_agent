"""联网检索并落库为草稿证据：会话工具与改稿流程共用同一口径。

单独成模块的原因与 `source_refresh` 相同：避免工具层反向导入工作流/服务层造成循环依赖。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.services.plain_text import enrich_search_query, query_has_no_subject
from app.services.search_planning import compile_search_plan_queries
from app.tools.search_tools import ExaMcpSearchError, ExaMcpSearchTool

logger = logging.getLogger("news_agent.evidence_search")

MAX_AGENT_QUERIES = 2


def _clean_queries(queries: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for query in queries:
        raw = " ".join(str(query).split()).strip()[:300]
        value = enrich_search_query(raw) if raw and not query_has_no_subject(raw) else ""
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
        if len(cleaned) == MAX_AGENT_QUERIES:
            break
    return cleaned


async def search_and_store_evidence(
    settings: Settings,
    repository,
    draft: Any,
    plans: list[dict[str, str]] | None = None,
    queries: list[str] | None = None,
) -> dict[str, Any]:
    """执行已校验的检索计划 → 并入草稿证据（带联网标记）→ 返回本次事实。

    `plans` 是新契约：模型只提出主体与信息缺口，服务端编译最终检索词；
    `queries` 仅兼容历史会话调用。失败只返回 `status="failed"`，不阻断主流程。
    """
    plan_queries, decisions = compile_search_plan_queries(plans)
    # 传入 plans（包括空列表）代表模型已经作出决定，不能再回退到旧裸字符串。
    cleaned = plan_queries if plans is not None else _clean_queries(queries or [])
    facts: dict[str, Any] = {
        "status": "ok", "queries": cleaned, "entries": 0, "titles": [], "search_plan": decisions,
    }
    if not cleaned:
        return {**facts, "status": "rejected"}
    if not (settings.exa_mcp_enabled and getattr(settings, "revision_search_enabled", True)):
        return {**facts, "status": "failed", "error": "联网检索未启用（EXA_MCP_ENABLED / REVISION_SEARCH_ENABLED）"}
    try:
        evidence = await ExaMcpSearchTool(settings).search(cleaned)
    except ExaMcpSearchError as exc:
        logger.warning("agent_search_failed draft_id=%s error_type=%s", draft.id, type(exc).__name__)
        return {**facts, "status": "failed", "error": str(exc)}
    appended = repository.append_draft_evidence(
        draft.id, evidence, origin="agent_search", prefix="agent-search",
    )
    titles = [str(item.get("title") or "")[:60] for item in evidence][:3]
    logger.info(
        "agent_search_evidence_stored draft_id=%s queries=%s entries=%s",
        draft.id, cleaned, appended,
    )
    return {
        **facts,
        "entries": appended,
        "titles": titles,
        "evidence_ids": [str(item.get("id")) for item in evidence],
    }
