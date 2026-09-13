from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.storage.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class SourceItemRow(Base):
    __tablename__ = "source_items"
    __table_args__ = (
        UniqueConstraint("source_kind", "external_id", name="uq_source_external_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_kind: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(500))
    source_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(2048))
    author: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    duplicate_of_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(80), index=True)
    category_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    hot_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    metrics_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # 同一来源可保留废弃历史并重新生成新的草稿版本。
    drafts: Mapped[list[DraftRow]] = relationship(back_populates="source_item")


class DraftRow(Base):
    __tablename__ = "content_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_item_id: Mapped[str] = mapped_column(
        ForeignKey("source_items.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(40), default="pending_review", index=True)
    title_options_json: Mapped[list] = mapped_column(JSONB, default=list)
    summary_cn: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[list] = mapped_column(JSONB, default=list)
    card_script_json: Mapped[list] = mapped_column(JSONB, default=list)
    source_name: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(String(2048))
    evidence_json: Mapped[list] = mapped_column(JSONB, default=list)
    content_plan_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    quality_report_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    claim_citations_json: Mapped[list] = mapped_column(JSONB, default=list)
    generation_mode: Mapped[str] = mapped_column(String(40), default="baseline")
    published_platform: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    source_item: Mapped[SourceItemRow] = relationship(back_populates="drafts")
    reviews: Mapped[list[ReviewEventRow]] = relationship(back_populates="draft")


class DraftSourceSnapshotRow(Base):
    """草稿生成期的私有来源正文快照；审核通过后只保留摘要元数据。"""

    __tablename__ = "draft_source_snapshots"
    __table_args__ = (UniqueConstraint("draft_id", name="uq_draft_source_snapshot_draft"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), index=True
    )
    source_item_id: Mapped[str] = mapped_column(
        ForeignKey("source_items.id", ondelete="CASCADE"), index=True
    )
    source_url: Mapped[str] = mapped_column(String(2048))
    content_origin: Mapped[str] = mapped_column(String(80), default="github_readme")
    object_key: Mapped[str | None] = mapped_column(String(500), unique=True, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    content_length: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    content_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PublicationRecordRow(Base):
    """人工回填的真实发布结果；本项目不保存账号或调用发布平台。"""

    __tablename__ = "publication_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), index=True
    )
    operator: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(100))
    published_url: Mapped[str] = mapped_column(String(2048))
    note: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublicationAssetRow(Base):
    """发布页专属私有图片素材，不与聊天会话或消息绑定。"""

    __tablename__ = "publication_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DraftPublicationAssetRow(Base):
    """运营人员在发布页为某篇草稿显式选择的私有素材。

    素材本身可保留，归属关系可用于按文章隔离显示，避免把历史上传全部
    混进当前文章的封面和正文插图选择器。
    """

    __tablename__ = "draft_publication_assets"
    __table_args__ = (
        UniqueConstraint("draft_id", "asset_id", name="uq_draft_publication_asset"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("publication_assets.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DraftIllustrationRow(Base):
    """草稿插图与位置审计；图片仍只保存在私有发布素材库。"""

    __tablename__ = "draft_illustrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("publication_assets.id", ondelete="RESTRICT"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(20), default="inline")
    placement_after_paragraph: Mapped[int] = mapped_column(Integer, default=1)
    prompt: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImageGenerationJobRow(Base):
    """单张自动配图的后台任务审计；任务 ID 可供前端独立轮询。"""

    __tablename__ = "image_generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chat_agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="CASCADE"), index=True
    )
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(20), default="inline")
    placement_after_paragraph: Mapped[int] = mapped_column(Integer, default=1)
    # 规划阶段选中的风格（文章级）与实物（每张图）；为空表示按草稿 ID 轮换（历史任务与未启用规划时）。
    style: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    arq_job_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    illustration_id: Mapped[str | None] = mapped_column(
        ForeignKey("draft_illustrations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AutoReviewRunRow(Base):
    """AI 自动审核的每次结论；不存储模型原始推理文本。"""

    __tablename__ = "auto_review_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), index=True
    )
    chat_agent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), index=True)
    rule_report_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    model_report_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    wechat_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DraftRevisionRow(Base):
    """自动审核驱动的改稿快照；保留可追溯版本，不保存模型原始推理。"""

    __tablename__ = "draft_revisions"
    __table_args__ = (
        UniqueConstraint("draft_id", "version", name="uq_draft_revision_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), index=True
    )
    auto_review_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("auto_review_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    summary_cn: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[list] = mapped_column(JSONB, default=list)
    revision_reason_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WechatPublicationJobRow(Base):
    """公众号草稿箱投递审计；不保存 AppSecret 或访问令牌。"""

    __tablename__ = "wechat_publication_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(40), default="cover_uploaded", index=True)
    cover_attachment_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_attachments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    cover_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("publication_assets.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    cover_media_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    inline_attachment_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    inline_asset_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    inline_image_urls_json: Mapped[list] = mapped_column(JSONB, default=list)
    wechat_draft_media_id: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)
    wechat_publish_id: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)
    published_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    submit_idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProjectIntroductionRow(Base):
    """已完成项目介绍的审计记录；唯一约束避免重复选题。"""

    __tablename__ = "project_introductions"
    __table_args__ = (
        UniqueConstraint("source_kind", "external_id", name="uq_project_introduction_source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_kind: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(500))
    source_item_id: Mapped[str] = mapped_column(
        ForeignKey("source_items.id", ondelete="CASCADE"), unique=True, index=True
    )
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), unique=True, index=True
    )
    publication_id: Mapped[str | None] = mapped_column(
        ForeignKey("publication_records.id", ondelete="CASCADE"), unique=True, nullable=True,
        index=True,
    )
    introduced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewEventRow(Base):
    __tablename__ = "review_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(30))
    reviewer: Mapped[str] = mapped_column(String(100))
    note: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    draft: Mapped[DraftRow] = relationship(back_populates="reviews")


class CollectionRunRow(Base):
    __tablename__ = "collection_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRunRow(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    action: Mapped[str] = mapped_column(String(40), default="collect")
    requested_sources_json: Mapped[list] = mapped_column(JSONB, default=list)
    limit: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), index=True)
    tool_results_json: Mapped[list] = mapped_column(JSONB, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatSessionRow(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ChatSessionMemoryRow(Base):
    """会话业务记忆：与 LangGraph checkpoint 配合，保存可验证的当前工作对象。"""

    __tablename__ = "chat_session_memories"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    active_draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    active_attachment_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_attachments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AttachmentRow(Base):
    __tablename__ = "chat_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30), default="uploaded", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AttachmentProcessingRow(Base):
    __tablename__ = "attachment_processing_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attachment_id: Mapped[str] = mapped_column(
        ForeignKey("chat_attachments.id", ondelete="CASCADE"), index=True
    )
    request_message_id: Mapped[str] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), index=True)
    draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="SET NULL"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatAgentRunRow(Base):
    __tablename__ = "chat_agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    request_message_id: Mapped[str] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True
    )
    response_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    intent: Mapped[str] = mapped_column(String(50), index=True)
    auto_review_requested: Mapped[bool] = mapped_column(default=False)
    auto_illustration_requested: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(40), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    tool_results_json: Mapped[list] = mapped_column(JSONB, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 保留原生成记录的创建时间；同一记录重试时仅刷新本次尝试的开始时间。
    attempt_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatAgentEventRow(Base):
    __tablename__ = "chat_agent_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_chat_agent_event_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="completed")
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSettingRow(Base):
    """长期运行偏好（键值）：让 Agent 的“长期修改”落到数据库而不是每次重建都丢。

    首个使用者是文章排版偏好：封面是否同时作为正文首图、固定结尾图、是否保留文字尾注。
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(60), default="agent")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class NotificationRow(Base):
    """面向运营页面的失败通知；不替代底层任务和发布审计。"""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_notification_source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_type: Mapped[str] = mapped_column(String(50), index=True)
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="error")
    target_view: Mapped[str] = mapped_column(String(30), default="review")
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    is_read: Mapped[bool] = mapped_column(default=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SchedulePlanRow(Base):
    __tablename__ = "schedule_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    chat_agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="CASCADE"), index=True
    )
    schedule_text: Mapped[str] = mapped_column(String(500))
    task_summary: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(40), default="pending_confirmation", index=True)
    confirmation_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PublishPlanRow(Base):
    __tablename__ = "publish_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    chat_agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="CASCADE"), index=True
    )
    draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_drafts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    platform: Mapped[str] = mapped_column(String(100))
    request_summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="pending_confirmation", index=True)
    confirmation_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TrendingSnapshotRow(Base):
    __tablename__ = "github_trending_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "collection_run_id",
            "source_item_id",
            "period",
            name="uq_trending_snapshot_run_item_period",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    collection_run_id: Mapped[str] = mapped_column(
        ForeignKey("collection_runs.id", ondelete="CASCADE"), index=True
    )
    source_item_id: Mapped[str] = mapped_column(
        ForeignKey("source_items.id", ondelete="CASCADE"), index=True
    )
    period: Mapped[str] = mapped_column(String(20), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    stars_period: Mapped[int] = mapped_column(Integer, default=0)
    stars_total: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
