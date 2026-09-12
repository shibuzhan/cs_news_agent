from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class SourceKind(StrEnum):
    ARXIV = "arxiv"
    GITHUB = "github"
    HACKER_NEWS = "hacker_news"
    RSS = "rss"
    ATTACHMENT = "attachment"


class ContentCategory(StrEnum):
    RESEARCH = "论文研究"
    OPEN_SOURCE = "开源项目"
    INDUSTRY_NEWS = "行业新闻"
    PRODUCT_UPDATE = "产品更新"
    DAILY_OBSERVATION = "日常观察"
    NEEDS_REVIEW = "待人工判断"


class ReviewStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    NEEDS_REVISION = "needs_revision"
    READY_TO_PUBLISH = "ready_to_publish"
    DRAFTBOX_CREATED = "draftbox_created"
    PUBLISHED = "published"
    DISCARDED = "discarded"
    DELETED = "deleted"


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class AttachmentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class ConversationIntent(StrEnum):
    GENERAL_CHAT = "general_chat"
    COLLECT_NEWS = "collect_news"
    CREATE_SCHEDULE_PLAN = "create_schedule_plan"
    CREATE_PUBLISH_PLAN = "create_publish_plan"
    ATTACHMENT_DRAFT = "attachment_draft"
    GENERATE_DRAFT_IMAGE = "generate_draft_image"
    REGENERATE_DRAFT = "regenerate_draft"


class ConversationRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    WAITING_CONFIRMATION = "waiting_confirmation"
    FAILED = "failed"


class PlanStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class RawSourceItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_kind: SourceKind
    external_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=1000)
    url: HttpUrl
    author: str | None = Field(default=None, max_length=500)
    published_at: datetime | None = None
    summary: str = ""
    content: str = ""
    source_name: str = Field(min_length=1, max_length=200)
    metrics: dict[str, int | float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedItem(RawSourceItem):
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=5000)
    content: str = Field(default="", max_length=500000)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    category: ContentCategory = ContentCategory.NEEDS_REVIEW
    category_confidence: float = Field(default=0.0, ge=0, le=1)
    hot_score: float = Field(default=0.0, ge=0)


class DraftContent(BaseModel):
    title_options: list[str] = Field(min_length=1, max_length=3)
    summary_cn: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1, max_length=10000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    card_script: list[str] = Field(default_factory=list, max_length=6)
    source_name: str
    source_url: HttpUrl
    content_plan: dict[str, Any] = Field(default_factory=dict)
    quality_report: dict[str, Any] = Field(default_factory=dict)
    claim_citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_pack: list[dict[str, Any]] = Field(default_factory=list)
    generation_mode: str = "baseline"


class ReviewCommand(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    action: str = Field(pattern=r"^(approve|reject|revoke|discard)$")
    note: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ManualPublicationCommand(BaseModel):
    """运营人员在目标平台实际发布后回填的审计信息，不执行平台操作。"""

    operator: str = Field(default="运营人员", min_length=1, max_length=100)
    platform: str = Field(default="手动发布", min_length=1, max_length=100)
    published_url: HttpUrl
    note: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=100)


class DraftEdit(BaseModel):
    title_options: list[str] | None = Field(default=None, min_length=1, max_length=3)
    summary_cn: str | None = Field(default=None, min_length=1, max_length=1000)
    body: str | None = Field(default=None, min_length=1, max_length=10000)
    tags: list[str] | None = Field(default=None, max_length=10)
    card_script: list[str] | None = Field(default=None, max_length=6)
    category: ContentCategory | None = None


class AgentCollectCommand(BaseModel):
    """受控主 Agent 的结构化指令，不接受自然语言或任意工具名。"""

    action: Literal["collect"] = "collect"
    sources: list[SourceKind] = Field(default_factory=list, max_length=4)
    limit: int = Field(default=25, ge=1, le=50)

    @model_validator(mode="after")
    def reject_duplicate_sources(self) -> AgentCollectCommand:
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources 不能包含重复来源")
        return self

    @property
    def requested_source_names(self) -> list[str]:
        return [source.value for source in self.sources]


class AgentRunResult(BaseModel):
    id: str
    action: str
    requested_sources: list[SourceKind]
    status: AgentRunStatus
    received: int
    created: int
    duplicates: int
    skipped: int
    source_errors: list[dict[str, str]] = Field(default_factory=list)
    runs: list[dict[str, Any]] = Field(default_factory=list)


class ChatSessionCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=200)


class ChatMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    attachment_id: str | None = Field(default=None, max_length=36)
    auto_review: bool = False
    auto_illustration: bool = False


class ConversationDecision(BaseModel):
    intent: ConversationIntent
    reply: str = Field(min_length=1, max_length=4000)
    sources: list[SourceKind] = Field(default_factory=list, max_length=4)
    limit: int = Field(default=5, ge=1, le=20)
    schedule_text: str | None = Field(default=None, max_length=500)
    platform: str | None = Field(default=None, max_length=100)
    draft_id: str | None = Field(default=None, max_length=36)
    auto_illustration: bool = False
    image_purpose: Literal["cover", "inline"] = "inline"
    placement_after_paragraph: int = Field(default=1, ge=0, le=20)


class DraftIllustrationCreate(BaseModel):
    asset_id: str = Field(min_length=1, max_length=36)
    placement_after_paragraph: int = Field(default=1, ge=0, le=20)
    purpose: Literal["cover", "inline"] = "inline"


class PlanConfirmationCommand(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)
