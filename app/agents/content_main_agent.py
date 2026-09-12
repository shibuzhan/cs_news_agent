from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.domain.models import (
    AgentCollectCommand,
    AgentRunResult,
    AgentRunStatus,
    SourceKind,
)
from app.services.collection import CollectionBatch, persist_collection_batch
from app.services.generator import DraftGenerator
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository
from app.tools.contracts import CollectionToolRequest
from app.tools.source_tools import SourceCollectionTool


logger = logging.getLogger("news_agent.content_main_agent")


class AgentCommandError(ValueError):
    pass


class MainAgentState(TypedDict, total=False):
    command: dict[str, Any]
    agent_run_id: str
    selected_sources: list[str]
    batches: list[CollectionBatch]
    tool_results: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    status: str
    error_message: str | None


PersistBatch = Callable[[CollectionBatch, DraftGenerator, str | None], dict[str, Any]]
CreateRun = Callable[[AgentCollectCommand], str]
FinishRun = Callable[[str, AgentRunStatus, list[dict[str, Any]], str | None], None]
GitHubCandidateFilter = Callable[[list[Any]], tuple[list[Any], int]]


def create_agent_run(command: AgentCollectCommand) -> str:
    with SessionLocal() as session:
        run = ContentRepository(session).create_agent_run(command)
        session.commit()
        return run.id


def finish_agent_run(
    run_id: str,
    status: AgentRunStatus,
    tool_results: list[dict[str, Any]],
    error_message: str | None,
) -> None:
    with SessionLocal() as session:
        ContentRepository(session).finish_agent_run(
            run_id, status, tool_results, error_message
        )
        session.commit()


def select_unintroduced_github_projects(items: list[Any]) -> tuple[list[Any], int]:
    """在排序前排除已发布或已有有效草稿的仓库，随后只选最高热度候选。"""
    if not items:
        return [], 0
    external_ids = [item.external_id for item in items]
    with SessionLocal() as session:
        excluded = ContentRepository(session).deduplicating_github_candidate_external_ids(
            external_ids
        )
    candidates = [item for item in items if item.external_id not in excluded]
    candidates.sort(
        key=lambda item: (
            -int(item.metrics.get("stars_period", 0)),
            -int(item.metrics.get("stars_total", 0)),
            min(item.metadata.get("periods", {}).values(), default=9999),
            item.external_id,
        )
    )
    return candidates[:1], len(excluded)


class ContentMainAgent:
    """只从命令中选择已注册 Tool 的受控采集编排器。"""

    def __init__(
        self,
        tools: dict[SourceKind, SourceCollectionTool],
        generator: DraftGenerator,
        persist_batch: PersistBatch = persist_collection_batch,
        create_run: CreateRun = create_agent_run,
        finish_run: FinishRun = finish_agent_run,
        github_candidate_filter: GitHubCandidateFilter = select_unintroduced_github_projects,
        settings: Settings | None = None,
    ):
        self.tools = tools
        self.generator = generator
        self.persist_batch = persist_batch
        self.create_run = create_run
        self.finish_run = finish_run
        self.github_candidate_filter = github_candidate_filter
        self.settings = settings
        self.graph = self._build_graph()

    def _build_graph(self):
        def plan_node(state: MainAgentState) -> MainAgentState:
            command = AgentCollectCommand.model_validate(state["command"])
            requested = command.sources or list(self.tools)
            unavailable = [source.value for source in requested if source not in self.tools]
            if unavailable:
                raise AgentCommandError(
                    f"来源不可用：{', '.join(unavailable)}"
                )
            return {"selected_sources": [source.value for source in requested]}

        async def execute_tools_node(state: MainAgentState) -> MainAgentState:
            command = AgentCollectCommand.model_validate(state["command"])
            batches: list[CollectionBatch] = []
            tool_results: list[dict[str, Any]] = []
            for source_name in state["selected_sources"]:
                source = SourceKind(source_name)
                tool = self.tools[source]
                batch = await tool.invoke(
                    CollectionToolRequest(source=source, limit=command.limit)
                )
                batches.append(batch)
                tool_results.append(tool.result_from_batch(batch).model_dump(mode="json"))
            return {"batches": batches, "tool_results": tool_results}

        async def before_enrichment_node(state: MainAgentState) -> MainAgentState:
            """GitHub README Tool 前执行持久化介绍历史检查，最多保留一个候选。"""
            batches: list[CollectionBatch] = []
            tool_results = list(state["tool_results"])
            for batch in state["batches"]:
                if batch.source_kind != SourceKind.GITHUB.value or batch.error:
                    batches.append(batch)
                    continue
                selected, excluded_before_rank = await asyncio.to_thread(
                    self.github_candidate_filter, batch.items
                )
                metadata = {
                    "candidate_count": len(batch.items),
                    "excluded_before_rank": excluded_before_rank,
                    "selected_project": selected[0].external_id if selected else None,
                }
                batches.append(
                    replace(batch, selected_items=selected, selection_metadata=metadata)
                )
                for result in tool_results:
                    if result.get("source") == SourceKind.GITHUB.value:
                        result["before_enrichment"] = metadata
            return {"batches": batches, "tool_results": tool_results}

        async def enrich_github_node(state: MainAgentState) -> MainAgentState:
            """已选项目才允许调用 README 富化 Tool，避免对榜单批量请求。"""
            batches: list[CollectionBatch] = []
            tool_results = list(state["tool_results"])
            for batch in state["batches"]:
                if batch.source_kind != SourceKind.GITHUB.value or batch.error:
                    batches.append(batch)
                    continue
                enriched = await self.tools[SourceKind.GITHUB].enrich_items(
                    batch.selected_items or []
                )
                metadata = {
                    **batch.selection_metadata,
                    "readme_requested": len(batch.selected_items or []),
                    "readme_available": sum(
                        item.metadata.get("readme_fetch_status") == "success"
                        for item in enriched
                    ),
                }
                batches.append(
                    replace(batch, selected_items=enriched, selection_metadata=metadata)
                )
                for result in tool_results:
                    if result.get("source") == SourceKind.GITHUB.value:
                        result["readme_enrichment"] = {
                            "requested": metadata["readme_requested"],
                            "available": metadata["readme_available"],
                        }
            return {"batches": batches, "tool_results": tool_results}

        async def persist_node(state: MainAgentState) -> MainAgentState:
            runs: list[dict[str, Any]] = []
            for batch in state["batches"]:
                args: tuple[object, ...] = (batch, self.generator, state["agent_run_id"])
                if self.settings is not None:
                    args = (*args, self.settings)
                run = await asyncio.to_thread(self.persist_batch, *args)
                runs.append(run)
            return {"runs": runs}

        def summarize_node(state: MainAgentState) -> MainAgentState:
            runs = state["runs"]
            completed = [run for run in runs if run["status"] != "failed"]
            created = sum(int(run.get("created", 0)) for run in runs)
            has_item_errors = any(run.get("errors") for run in runs)
            status = (
                AgentRunStatus.SUCCESS.value
                if all(run["status"] == "success" for run in runs)
                else AgentRunStatus.PARTIAL.value
                if completed
                else AgentRunStatus.FAILED.value
            )
            # 来源虽然请求成功但全部在生成阶段失败时，不能误报“部分完成”。
            if created == 0 and has_item_errors:
                status = AgentRunStatus.FAILED.value
            errors = [
                run.get("error") or run.get("error_message")
                for run in runs
                if run["status"] != "success"
            ]
            errors.extend(
                error.get("error", "")
                for run in runs
                for error in run.get("errors", [])
                if isinstance(error, dict)
            )
            return {
                "status": status,
                "error_message": "; ".join(error for error in errors if error) or None,
            }

        graph = StateGraph(MainAgentState)
        graph.add_node("plan", plan_node)
        graph.add_node("execute_tools", execute_tools_node)
        graph.add_node("before_enrichment", before_enrichment_node)
        graph.add_node("enrich_github", enrich_github_node)
        graph.add_node("persist", persist_node)
        graph.add_node("summarize", summarize_node)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "execute_tools")
        graph.add_edge("execute_tools", "before_enrichment")
        graph.add_edge("before_enrichment", "enrich_github")
        graph.add_edge("enrich_github", "persist")
        graph.add_edge("persist", "summarize")
        graph.add_edge("summarize", END)
        return graph.compile()

    async def run(self, command: AgentCollectCommand) -> AgentRunResult:
        agent_run_id = self.create_run(command)
        requested = command.sources or list(self.tools)
        logger.info(
            "content_agent_started agent_run_id=%s sources=%s limit=%s",
            agent_run_id,
            [source.value for source in requested],
            command.limit,
        )
        try:
            state = await self.graph.ainvoke(
                {
                    "command": command.model_dump(mode="json"),
                    "agent_run_id": agent_run_id,
                }
            )
            status = AgentRunStatus(state["status"])
            self.finish_run(
                agent_run_id,
                status,
                state["runs"],
                state.get("error_message"),
            )
            logger.info(
                "content_agent_finished agent_run_id=%s status=%s created=%s received=%s",
                agent_run_id,
                status.value,
                sum(run.get("created", 0) for run in state["runs"]),
                sum(run.get("received", 0) for run in state["runs"]),
            )
            return AgentRunResult(
                id=agent_run_id,
                action=command.action,
                requested_sources=requested,
                status=status,
                received=sum(run.get("received", 0) for run in state["runs"]),
                created=sum(run.get("created", 0) for run in state["runs"]),
                duplicates=sum(run.get("duplicates", 0) for run in state["runs"]),
                skipped=sum(run.get("skipped", 0) for run in state["runs"]),
                source_errors=[
                    {
                        "source": run["source"],
                        "error": run.get("error") or run.get("error_message") or "; ".join(
                            str(error.get("error", "")) for error in run.get("errors", []) if isinstance(error, dict)
                        ),
                    }
                    for run in state["runs"]
                    if run["status"] != "success" or run.get("errors")
                ],
                runs=state["runs"],
            )
        except Exception as exc:
            logger.exception(
                "content_agent_failed agent_run_id=%s error_type=%s",
                agent_run_id,
                type(exc).__name__,
            )
            self.finish_run(
                agent_run_id,
                AgentRunStatus.FAILED,
                [],
                str(exc),
            )
            raise
