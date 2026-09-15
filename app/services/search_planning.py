"""联网检索计划的共享契约：模型提出需求，服务端决定是否可执行。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.services.plain_text import build_search_plan_query


class SearchPlanItem(BaseModel):
    """一条可审计的检索需求，不包含模型可任意执行的原始查询。"""

    subject: str = Field(min_length=2, max_length=120)
    need: str = Field(min_length=2, max_length=120)
    reason: str = Field(default="", max_length=240)
    preferred_source: Literal["official_docs", "repository", "authoritative_web"] = "official_docs"

    @field_validator("subject", "need", "reason", mode="before")
    @classmethod
    def _normalize_text(cls, value: object) -> str:
        return " ".join(str(value or "").split())


def normalize_search_plans(value: object) -> list[SearchPlanItem]:
    """忽略无效、重复计划；每轮最多允许两个具体信息缺口。"""
    if not isinstance(value, list):
        return []
    plans: list[SearchPlanItem] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        try:
            plan = SearchPlanItem.model_validate(item)
        except ValidationError:
            continue
        key = (plan.subject.casefold(), plan.need.casefold())
        if key in seen:
            continue
        seen.add(key)
        plans.append(plan)
        if len(plans) == 2:
            break
    return plans


def compile_search_plan_queries(value: object) -> tuple[list[str], list[dict]]:
    """将已验证的计划编译为最终检索词，并保留每项接受/拒绝事实。"""
    queries: list[str] = []
    decisions: list[dict] = []
    for plan in normalize_search_plans(value):
        query = build_search_plan_query(plan.subject, plan.need, plan.preferred_source)
        decisions.append(
            {
                "subject": plan.subject,
                "need": plan.need,
                "reason": plan.reason,
                "preferred_source": plan.preferred_source,
                "query": query,
                "accepted": bool(query),
            }
        )
        if query and query not in queries:
            queries.append(query)
    return queries, decisions
