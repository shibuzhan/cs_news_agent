from __future__ import annotations

from dataclasses import dataclass

from app.storage.repositories import ContentRepository
from app.storage.tables import PublishPlanRow, SchedulePlanRow


@dataclass(frozen=True)
class CreateSchedulePlanRequest:
    session_id: str
    run_id: str
    schedule_text: str
    task_summary: str
    sources: list[str]


class CreateSchedulePlanTool:
    """仅创建待确认计划；不注册或执行定时任务。"""

    def invoke(
        self, repository: ContentRepository, request: CreateSchedulePlanRequest
    ) -> SchedulePlanRow:
        return repository.create_schedule_plan(
            session_id=request.session_id,
            run_id=request.run_id,
            schedule_text=request.schedule_text,
            task_summary=request.task_summary,
            sources=request.sources,
        )


@dataclass(frozen=True)
class CreatePublishPlanRequest:
    session_id: str
    run_id: str
    platform: str
    request_summary: str
    draft_id: str | None = None


class CreatePublishPlanTool:
    """仅创建待确认发布计划；不保存账号且不调用发布平台。"""

    def invoke(
        self, repository: ContentRepository, request: CreatePublishPlanRequest
    ) -> PublishPlanRow:
        return repository.create_publish_plan(
            session_id=request.session_id,
            run_id=request.run_id,
            platform=request.platform,
            request_summary=request.request_summary,
            draft_id=request.draft_id,
        )


@dataclass(frozen=True)
class ConfirmPlanRequest:
    plan_id: str
    idempotency_key: str


class ConfirmSchedulePlanTool:
    """仅确认计划记录；不启动调度器。"""

    def invoke(self, repository: ContentRepository, request: ConfirmPlanRequest) -> SchedulePlanRow:
        return repository.confirm_schedule_plan(request.plan_id, request.idempotency_key)


class ConfirmPublishPlanTool:
    """仅确认发布计划；不调用任何发布平台。"""

    def invoke(self, repository: ContentRepository, request: ConfirmPlanRequest) -> PublishPlanRow:
        return repository.confirm_publish_plan(request.plan_id, request.idempotency_key)
