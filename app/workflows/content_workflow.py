from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.models import DraftContent, NormalizedItem, RawSourceItem
from app.services.classifier import classify_item
from app.services.generator import DraftGenerator
from app.services.model_errors import README_UNAVAILABLE_REASON, safe_failure_reason
from app.services.normalizer import has_meaningful_content, normalize_item
from app.services.ranker import calculate_hot_score
from app.storage.repositories import ContentRepository
from app.services.source_snapshots import DraftSourceSnapshotStore

# 与 GitHub 采集器保持一致的最小 README 长度。
MIN_README_CHARS = 200

logger = logging.getLogger("news_agent.content_workflow")


def _github_content_ready(raw: RawSourceItem) -> bool:
    """GitHub 条目必须有可用 README 正文才允许生成。

    仅有的 Trending 简介通常只有一两百字，在正文下限下只能靠“来源没有说明”这类空话凑数，
    因此这里直接拒绝生成，并把原因交给失败详情展示。
    """
    metadata = raw.metadata if isinstance(raw.metadata, dict) else {}
    if metadata.get("readme_fetch_status") != "success":
        return False
    return len((raw.content or "").strip()) >= MIN_README_CHARS


class ContentState(TypedDict, total=False):
    raw_item: dict[str, Any]
    normalized_item: dict[str, Any]
    eligible: bool
    skip_reason: str
    draft: dict[str, Any]


def build_content_graph(generator: DraftGenerator):
    def normalize_node(state: ContentState) -> ContentState:
        raw = RawSourceItem.model_validate(state["raw_item"])
        item = normalize_item(raw)
        return {"normalized_item": item.model_dump(mode="json")}

    def filter_node(state: ContentState) -> ContentState:
        item = NormalizedItem.model_validate(state["normalized_item"])
        eligible = has_meaningful_content(item)
        return {
            "eligible": eligible,
            "skip_reason": "内容信息量不足" if not eligible else "",
        }

    def classify_and_rank_node(state: ContentState) -> ContentState:
        item = NormalizedItem.model_validate(state["normalized_item"])
        result = classify_item(item)
        item.category = result.category
        item.category_confidence = result.confidence
        item.metadata["classification_reason"] = result.reason
        item.hot_score = calculate_hot_score(item)
        return {"normalized_item": item.model_dump(mode="json")}

    def generate_node(state: ContentState) -> ContentState:
        item = NormalizedItem.model_validate(state["normalized_item"])
        draft = generator.generate(item)
        return {"draft": draft.model_dump(mode="json")}

    graph = StateGraph(ContentState)
    graph.add_node("normalize", normalize_node)
    graph.add_node("filter", filter_node)
    graph.add_node("classify_and_rank", classify_and_rank_node)
    graph.add_node("generate", generate_node)
    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "filter")
    graph.add_conditional_edges(
        "filter",
        lambda state: "continue" if state["eligible"] else "skip",
        {"continue": "classify_and_rank", "skip": END},
    )
    graph.add_edge("classify_and_rank", "generate")
    graph.add_edge("generate", END)
    return graph.compile()


class ContentPipeline:
    def __init__(
        self, session: Session, generator: DraftGenerator, settings: Settings | None = None,
    ):
        self.session = session
        self.repository = ContentRepository(session)
        self.generator = generator
        # 生成器需要同一工作流会话中的短偏好；长文风格文件即使没有仓储也可独立读取。
        # 使用属性注入避免把数据库会话带入模型客户端或 Agent 构造阶段。
        if hasattr(generator, "repository"):
            generator.repository = self.repository
        self.graph = build_content_graph(generator)
        self.source_snapshots = (
            DraftSourceSnapshotStore(settings, self.repository) if settings is not None else None
        )

    def _capture_source_snapshot(self, draft_id: str, item: RawSourceItem) -> None:
        """仅为已成功获取的 GitHub README 创建草稿期私有副本。"""
        if self.source_snapshots is not None:
            self.source_snapshots.capture_github_readme(draft_id, item)

    def process(self, raw_items: list[RawSourceItem]) -> dict[str, Any]:
        result: dict[str, Any] = {"received": len(raw_items), "created": 0, "duplicates": 0, "skipped": 0, "errors": [], "created_draft_ids": []}
        for raw in raw_items:
            try:
                state = self.graph.invoke({"raw_item": raw.model_dump(mode="json")})
                if not state.get("eligible"):
                    result["skipped"] += 1
                    continue
                normalized = NormalizedItem.model_validate(state["normalized_item"])
                saved = self.repository.save_source(normalized)
                if self.repository.get_deduplicating_draft_for_source(saved.row.id):
                    result["duplicates"] += 1
                else:
                    draft = DraftContent.model_validate(state["draft"])
                    draft = self.repository.create_draft(
                        saved.row,
                        draft,
                        draft.evidence_pack
                        or [{"id": "source-1", "title": normalized.title, "url": str(normalized.url), "summary": normalized.summary[:500]}],
                    )
                    self._capture_source_snapshot(draft.id, normalized)
                    result["created"] += 1
                    result["created_draft_ids"].append(draft.id)
                self.session.commit()
            except Exception as exc:  # 单条失败不能使整批数据回滚
                self.session.rollback()
                result["errors"].append({"external_id": raw.external_id, "error": safe_failure_reason(exc)})
        return result

    def regenerate_draft(self, draft_id: str, raw: RawSourceItem) -> dict[str, Any]:
        """复用原草稿，不创建新的同源草稿记录。"""
        state = self.graph.invoke({"raw_item": raw.model_dump(mode="json")})
        if not state.get("eligible"):
            raise ValueError("重新获取的来源内容信息量不足")
        normalized = NormalizedItem.model_validate(state["normalized_item"])
        saved = self.repository.save_source(normalized)
        generated = DraftContent.model_validate(state["draft"])
        evidence = generated.evidence_pack or [{
            "id": "source-1",
            "title": normalized.title,
            "url": str(normalized.url),
            "summary": normalized.summary[:1000],
            "content_origin": normalized.metadata.get("content_origin"),
        }]
        draft = self._write_regenerated_draft(draft_id, generated, evidence)
        self._capture_source_snapshot(draft.id, normalized)
        return {"draft_id": draft.id, "version": draft.version, "source_item_id": saved.row.id}

    def _write_regenerated_draft(
        self, draft_id: str, generated: DraftContent, evidence: list[dict], attempts: int = 2,
    ) -> Any:
        """落库重生成正文；版本被并发写入抢先时用下一个版本号再写一次。

        模型调用已经花掉 9–12 分钟，绝不能让一次版本号冲突把结果丢掉：`draft_revisions`
        有 (draft_id, version) 唯一约束，另一个写者（例如并发的按意见改稿）先提交后，
        这里必须换号重写，而不是抛 IntegrityError 让整个任务失败。
        真实故障：用户看到“生成失败”，但草稿其实已经涨过版本，报告与内容对不上。
        """
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with self.session.begin_nested():
                    return self.repository.regenerate_draft(draft_id, generated, evidence)
            except IntegrityError as exc:
                last_error = exc
                logger.warning(
                    "draft_regeneration_version_conflict draft_id=%s attempt=%s",
                    draft_id, attempt,
                )
        raise last_error if last_error is not None else RuntimeError("重生成草稿写入失败")

    def save_github_candidates(self, raw_items: list[RawSourceItem]) -> None:
        """保存 Trending 候选和趋势快照所需字段，但不为它们批量生成草稿。"""
        for raw in raw_items:
            item = normalize_item(raw)
            classified = classify_item(item)
            item.category = classified.category
            item.category_confidence = classified.confidence
            item.hot_score = calculate_hot_score(item)
            self.repository.save_source(item)
        self.session.flush()

    def process_github_single(
        self, raw_items: list[RawSourceItem], candidate_count: int
    ) -> dict[str, Any]:
        """一个项目对应一篇草稿；调用方已在 README Tool 前做介绍历史筛选。"""
        result: dict[str, Any] = {
            "received": candidate_count,
            "selected": len(raw_items),
            "created": 0,
            "duplicates": 0,
            "skipped": max(candidate_count - len(raw_items), 0),
            "errors": [],
            "created_draft_ids": [],
        }
        for raw in raw_items:
            try:
                # 生成前先检查来源正文：README 缺失时只用 Trending 简介写长文只会得到空话。
                if not _github_content_ready(raw):
                    result["errors"].append(
                        {"external_id": raw.external_id, "error": README_UNAVAILABLE_REASON}
                    )
                    continue
                with self.session.begin_nested():
                    state = self.graph.invoke({"raw_item": raw.model_dump(mode="json")})
                    if not state.get("eligible"):
                        result["skipped"] += 1
                        continue
                    normalized = NormalizedItem.model_validate(state["normalized_item"])
                    saved = self.repository.save_source(normalized)
                    if self.repository.get_deduplicating_draft_for_source(saved.row.id):
                        result["duplicates"] += 1
                        continue
                    generated = DraftContent.model_validate(state["draft"])
                    draft = self.repository.create_draft(
                        saved.row,
                        generated,
                        generated.evidence_pack or [{
                            "id": "source-1",
                            "title": normalized.title,
                            "url": str(normalized.url),
                            "summary": normalized.summary[:1000],
                            "content_origin": normalized.metadata.get("content_origin"),
                        }],
                    )
                    self._capture_source_snapshot(draft.id, normalized)
                    result["created"] += 1
                    result["created_draft_ids"].append(draft.id)
            except Exception as exc:  # 单项目失败不能让候选快照回滚
                result["errors"].append({"external_id": raw.external_id, "error": safe_failure_reason(exc)})
        return result

    def process_github_aggregate(self, raw_items: list[RawSourceItem], top_n: int = 5) -> dict[str, Any]:
        """并行整理 GitHub 项目，再以一条聚合草稿保留全部证据。"""
        def prepare(raw: RawSourceItem) -> NormalizedItem | None:
            item = normalize_item(raw)
            if not has_meaningful_content(item):
                return None
            classified = classify_item(item)
            item.category = classified.category
            item.category_confidence = classified.confidence
            item.hot_score = calculate_hot_score(item)
            return item

        with ThreadPoolExecutor(max_workers=min(5, max(1, len(raw_items)))) as pool:
            prepared = list(pool.map(prepare, raw_items))
        items = [item for item in prepared if item is not None]
        selected = sorted(items, key=lambda item: item.hot_score, reverse=True)[:top_n]
        result: dict[str, Any] = {
            "received": len(raw_items), "created": 0, "duplicates": 0,
            "skipped": len(raw_items) - len(items), "errors": [], "aggregated": len(selected),
            "created_draft_ids": [],
        }
        if not selected:
            return result
        try:
            for item in items:
                saved = self.repository.save_source(item)
                if saved.is_duplicate:
                    result["duplicates"] += 1

            evidence = [
                {
                    "title": item.title,
                    "url": str(item.url),
                    "summary": item.summary[:300],
                    "stars_total": int(item.metrics.get("stars_total", 0)),
                    "stars_period": int(item.metrics.get("stars_period", 0)),
                }
                for item in selected
            ]
            fingerprint = hashlib.sha256(
                "|".join(item.external_id for item in selected).encode("utf-8")
            ).hexdigest()[:20]
            aggregate_raw = RawSourceItem(
                source_kind=selected[0].source_kind,
                external_id=f"github-trending-aggregate-{fingerprint}",
                title=f"GitHub Trending 热门项目速览｜TOP {len(selected)}",
                url="https://github.com/trending",
                published_at=datetime.now(UTC),
                summary="本期 GitHub Trending 热门项目聚合速览。",
                content=json.dumps(evidence, ensure_ascii=False),
                source_name="GitHub Trending",
                metrics={"project_count": len(selected)},
                metadata={"aggregate": True, "evidence": evidence},
            )
            aggregate = normalize_item(aggregate_raw)
            aggregate.category = selected[0].category
            aggregate.category_confidence = 1.0
            aggregate.hot_score = max(item.hot_score for item in selected)
            saved_aggregate = self.repository.save_source(aggregate)
            if self.repository.get_deduplicating_draft_for_source(saved_aggregate.row.id):
                result["duplicates"] += 1
            else:
                draft = self.generator.generate(aggregate)
                created_draft = self.repository.create_draft(
                    saved_aggregate.row, draft, draft.evidence_pack or evidence
                )
                result["created"] = 1
                if created_draft is not None and hasattr(created_draft, "id"):
                    result["created_draft_ids"].append(created_draft.id)
            self.session.commit()
        except Exception as exc:
            self.session.rollback()
            result["errors"].append({"external_id": "github-aggregate", "error": safe_failure_reason(exc)})
        return result
