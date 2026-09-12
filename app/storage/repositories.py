from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from hashlib import sha256
import logging
import re

from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.domain.models import (
    AgentCollectCommand,
    AgentRunStatus,
    AttachmentStatus,
    ConversationIntent,
    ConversationRunStatus,
    DraftContent,
    DraftEdit,
    ManualPublicationCommand,
    NormalizedItem,
    PlanStatus,
    ReviewCommand,
    ReviewStatus,
)
from app.storage.tables import (
    AgentRunRow,
    AttachmentProcessingRow,
    AutoReviewRunRow,
    AttachmentRow,
    ChatAgentEventRow,
    ChatAgentRunRow,
    ChatMessageRow,
    ChatSessionMemoryRow,
    ChatSessionRow,
    CollectionRunRow,
    DraftRow,
    DraftSourceSnapshotRow,
    DraftRevisionRow,
    DraftIllustrationRow,
    DraftPublicationAssetRow,
    ImageGenerationJobRow,
    NotificationRow,
    PublicationRecordRow,
    PublicationAssetRow,
    ProjectIntroductionRow,
    ReviewEventRow,
    PublishPlanRow,
    SchedulePlanRow,
    SourceItemRow,
    TrendingSnapshotRow,
    WechatPublicationJobRow,
    utcnow,
)
from app.services.plain_text import normalize_plain_text


logger = logging.getLogger("news_agent.repositories")

# 生成记录（任务队列）里可见的运行类型：生成、附件成稿、配图、自动审核与审核决定。
GENERATION_RECORD_INTENTS: tuple[str, ...] = (
    ConversationIntent.COLLECT_NEWS.value,
    ConversationIntent.ATTACHMENT_DRAFT.value,
    ConversationIntent.REGENERATE_DRAFT.value,
    ConversationIntent.GENERATE_DRAFT_IMAGE.value,
    ConversationIntent.RUN_AUTO_REVIEW.value,
    ConversationIntent.PUBLISH_TO_WECHAT_DRAFT.value,
    ConversationIntent.RESELECT_PUBLICATION_ASSETS.value,
    "approve_draft",
    "discard_draft",
    "revoke_approval",
)


class RepositoryError(RuntimeError):
    pass


class DraftNotFound(RepositoryError):
    pass


class InvalidReviewTransition(RepositoryError):
    pass


class ChatSessionNotFound(RepositoryError):
    pass


class AttachmentNotFound(RepositoryError):
    pass


class WechatPublicationNotFound(RepositoryError):
    pass


class PublicationAssetNotFound(RepositoryError):
    pass


@dataclass(frozen=True)
class SaveSourceResult:
    row: SourceItemRow
    is_new: bool
    is_duplicate: bool


class ContentRepository:
    def __init__(self, session: Session):
        self.session = session

    def save_source(self, item: NormalizedItem) -> SaveSourceResult:
        existing = self.session.scalar(
            select(SourceItemRow).where(
                SourceItemRow.source_kind == item.source_kind.value,
                SourceItemRow.external_id == item.external_id,
            )
        )
        if existing:
            existing.title = item.title
            existing.url = str(item.url)
            existing.summary = item.summary
            existing.content = item.content
            existing.published_at = item.published_at
            existing.hot_score = item.hot_score
            existing.metrics_json = item.metrics
            existing.metadata_json = item.metadata
            existing.fetched_at = utcnow()
            self.session.flush()
            return SaveSourceResult(existing, is_new=False, is_duplicate=True)

        canonical = self.session.scalar(
            select(SourceItemRow)
            .where(SourceItemRow.content_hash == item.content_hash)
            .order_by(SourceItemRow.created_at.asc())
            .limit(1)
        )
        row = SourceItemRow(
            source_kind=item.source_kind.value,
            external_id=item.external_id,
            source_name=item.source_name,
            title=item.title,
            url=str(item.url),
            author=item.author,
            published_at=item.published_at,
            summary=item.summary,
            content=item.content,
            content_hash=item.content_hash,
            duplicate_of_id=canonical.id if canonical else None,
            category=item.category.value,
            category_confidence=item.category_confidence,
            hot_score=item.hot_score,
            metrics_json=item.metrics,
            metadata_json=item.metadata,
        )
        self.session.add(row)
        self.session.flush()
        return SaveSourceResult(row, is_new=True, is_duplicate=canonical is not None)

    def create_collection_run(
        self,
        source_kind: str,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        item_count: int = 0,
        created_count: int = 0,
        duplicate_count: int = 0,
        skipped_count: int = 0,
        error_message: str | None = None,
        agent_run_id: str | None = None,
    ) -> CollectionRunRow:
        row = CollectionRunRow(
            agent_run_id=agent_run_id,
            source_kind=source_kind,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            item_count=item_count,
            created_count=created_count,
            duplicate_count=duplicate_count,
            skipped_count=skipped_count,
            error_message=error_message,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_agent_run(self, command: AgentCollectCommand) -> AgentRunRow:
        row = AgentRunRow(
            action=command.action,
            requested_sources_json=command.requested_source_names,
            limit=command.limit,
            status=AgentRunStatus.RUNNING.value,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_chat_session(self, title: str) -> ChatSessionRow:
        row = ChatSessionRow(title=title)
        self.session.add(row)
        self.session.flush()
        return row

    def list_chat_sessions(self, limit: int = 100) -> list[ChatSessionRow]:
        return list(
            self.session.scalars(
                select(ChatSessionRow)
                .order_by(ChatSessionRow.updated_at.desc())
                .limit(limit)
            )
        )

    def get_chat_session(self, session_id: str) -> ChatSessionRow:
        row = self.session.get(ChatSessionRow, session_id)
        if row is None:
            raise ChatSessionNotFound(f"对话不存在：{session_id}")
        return row

    def delete_chat_session(self, session_id: str) -> None:
        row = self.get_chat_session(session_id)
        self.session.delete(row)
        self.session.flush()

    def create_chat_message(
        self, session_id: str, role: str, content: str
    ) -> ChatMessageRow:
        session_row = self.get_chat_session(session_id)
        session_row.updated_at = utcnow()
        row = ChatMessageRow(session_id=session_id, role=role, content=content)
        self.session.add(row)
        self.session.flush()
        return row

    def set_first_instruction_title(self, session_id: str, content: str) -> ChatSessionRow:
        """仅首条真实对话指令命名；附件上传生成的系统消息不参与。"""
        row = self.get_chat_session(session_id)
        if row.title != "新对话":
            return row
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        first_sentence = re.split(r"[。！？!?]", first_line, maxsplit=1)[0].strip()
        if not first_sentence:
            return row
        row.title = first_sentence[:36].rstrip() + ("…" if len(first_sentence) > 36 else "")
        self.session.flush()
        return row

    def update_chat_message(self, message_id: str, content: str) -> ChatMessageRow:
        row = self.session.get(ChatMessageRow, message_id)
        if row is None:
            raise RepositoryError(f"聊天消息不存在：{message_id}")
        row.content = content
        self.get_chat_session(row.session_id).updated_at = utcnow()
        self.session.flush()
        return row

    def list_chat_messages(self, session_id: str) -> list[ChatMessageRow]:
        self.get_chat_session(session_id)
        return list(
            self.session.scalars(
                select(ChatMessageRow)
                .where(ChatMessageRow.session_id == session_id)
                .order_by(ChatMessageRow.created_at.asc())
            )
        )

    def get_chat_session_memory(self, session_id: str) -> ChatSessionMemoryRow:
        self.get_chat_session(session_id)
        row = self.session.get(ChatSessionMemoryRow, session_id)
        if row is None:
            row = ChatSessionMemoryRow(session_id=session_id)
            self.session.add(row)
            self.session.flush()
        return row

    def update_chat_session_memory(
        self,
        session_id: str,
        *,
        active_draft_id: str | None = None,
        active_attachment_id: str | None = None,
        summary: str | None = None,
        clear_draft: bool = False,
        clear_attachment: bool = False,
    ) -> ChatSessionMemoryRow:
        row = self.get_chat_session_memory(session_id)
        if active_draft_id is not None:
            self.get_draft(active_draft_id)
            row.active_draft_id = active_draft_id
        elif clear_draft:
            row.active_draft_id = None
        if active_attachment_id is not None:
            attachment = self.get_attachment(active_attachment_id)
            if attachment.session_id != session_id:
                raise AttachmentNotFound("附件不属于当前对话")
            row.active_attachment_id = active_attachment_id
        elif clear_attachment:
            row.active_attachment_id = None
        if summary is not None:
            row.summary = summary[:4000]
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def create_attachment(
        self,
        session_id: str,
        message_id: str | None,
        original_name: str,
        content_type: str,
        object_key: str,
        size_bytes: int,
        sha256: str,
    ) -> AttachmentRow:
        self.get_chat_session(session_id)
        row = AttachmentRow(
            session_id=session_id,
            message_id=message_id,
            original_name=original_name,
            content_type=content_type,
            object_key=object_key,
            size_bytes=size_bytes,
            sha256=sha256,
            status=AttachmentStatus.UPLOADED.value,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_attachment(self, attachment_id: str) -> AttachmentRow:
        row = self.session.get(AttachmentRow, attachment_id)
        if row is None:
            raise AttachmentNotFound(f"附件不存在：{attachment_id}")
        return row

    def list_attachments(self, session_id: str) -> list[AttachmentRow]:
        self.get_chat_session(session_id)
        return list(
            self.session.scalars(
                select(AttachmentRow)
                .where(AttachmentRow.session_id == session_id)
                .order_by(AttachmentRow.created_at.asc())
            )
        )

    def list_image_attachments(self, limit: int = 100) -> list[AttachmentRow]:
        """只返回图片元数据，实际文件仍需经私有对象存储读取。"""
        return list(
            self.session.scalars(
                select(AttachmentRow)
                .where(AttachmentRow.content_type.in_(("image/jpeg", "image/png")))
                .order_by(AttachmentRow.created_at.desc())
                .limit(limit)
            )
        )

    def create_publication_asset(
        self,
        original_name: str,
        content_type: str,
        object_key: str,
        size_bytes: int,
        sha256: str,
    ) -> PublicationAssetRow:
        row = PublicationAssetRow(
            original_name=original_name,
            content_type=content_type,
            object_key=object_key,
            size_bytes=size_bytes,
            sha256=sha256,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_publication_asset(self, asset_id: str) -> PublicationAssetRow:
        row = self.session.get(PublicationAssetRow, asset_id)
        if row is None:
            raise PublicationAssetNotFound("发布素材不存在")
        return row

    def list_publication_assets(self, limit: int = 100) -> list[PublicationAssetRow]:
        return list(
            self.session.scalars(
                select(PublicationAssetRow)
                .order_by(PublicationAssetRow.created_at.desc())
                .limit(limit)
            )
        )

    def bind_publication_asset(self, draft_id: str, asset_id: str) -> DraftPublicationAssetRow:
        """建立文章与发布素材的显式归属；重复绑定保持幂等。"""
        self.get_draft(draft_id)
        self.get_publication_asset(asset_id)
        existing = self.session.scalar(
            select(DraftPublicationAssetRow).where(
                DraftPublicationAssetRow.draft_id == draft_id,
                DraftPublicationAssetRow.asset_id == asset_id,
            )
        )
        if existing:
            return existing
        row = DraftPublicationAssetRow(draft_id=draft_id, asset_id=asset_id)
        self.session.add(row)
        self.session.flush()
        return row

    def list_draft_publication_assets(self, draft_id: str, limit: int = 100) -> list[PublicationAssetRow]:
        """返回当前文章显式上传或由 Agent 生成/插入的素材，不回填旧全局素材。"""
        self.get_draft(draft_id)
        bound_ids = set(
            self.session.scalars(
                select(DraftPublicationAssetRow.asset_id).where(
                    DraftPublicationAssetRow.draft_id == draft_id
                )
            )
        )
        bound_ids.update(
            self.session.scalars(
                select(DraftIllustrationRow.asset_id).where(
                    DraftIllustrationRow.draft_id == draft_id
                )
            )
        )
        if not bound_ids:
            return []
        return list(
            self.session.scalars(
                select(PublicationAssetRow)
                .where(PublicationAssetRow.id.in_(bound_ids))
                .order_by(PublicationAssetRow.created_at.desc())
                .limit(limit)
            )
        )

    def delete_publication_asset(self, asset_id: str) -> PublicationAssetRow:
        row = self.get_publication_asset(asset_id)
        jobs = list(self.session.scalars(select(WechatPublicationJobRow)))
        is_referenced = any(
            job.cover_asset_id == asset_id
            or asset_id in (job.inline_asset_ids_json or [])
            for job in jobs
        )
        illustration = self.session.scalar(
            select(DraftIllustrationRow.id).where(DraftIllustrationRow.asset_id == asset_id).limit(1)
        )
        if is_referenced or illustration:
            raise InvalidReviewTransition("该图片已被草稿插图或公众号投递记录引用，不能删除")
        # 仅作为当前文章待选素材时可以连同归属记录一起删除；不影响其他素材。
        for binding in self.session.scalars(
            select(DraftPublicationAssetRow).where(DraftPublicationAssetRow.asset_id == asset_id)
        ):
            self.session.delete(binding)
        self.session.delete(row)
        self.session.flush()
        return row

    def create_attachment_processing(
        self, attachment_id: str, request_message_id: str
    ) -> AttachmentProcessingRow:
        attachment = self.get_attachment(attachment_id)
        attachment.status = AttachmentStatus.PROCESSING.value
        row = AttachmentProcessingRow(
            attachment_id=attachment_id,
            request_message_id=request_message_id,
            status=AttachmentStatus.PROCESSING.value,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_chat_agent_run(
        self,
        session_id: str,
        request_message_id: str | None,
        intent: ConversationIntent | str,
        auto_review_requested: bool = False,
        auto_illustration_requested: bool = False,
    ) -> ChatAgentRunRow:
        self.get_chat_session(session_id)
        # 兼容枚举与字符串：Agent 工具里常常直接写字符串，取 .value 会抛
        # AttributeError（真实故障：投递工具在对话里报“模型暂时不可用”，实为 'str' has no 'value'）。
        intent_value = intent.value if isinstance(intent, ConversationIntent) else str(intent)
        row = ChatAgentRunRow(
            session_id=session_id,
            request_message_id=request_message_id,
            intent=intent_value,
            auto_review_requested=auto_review_requested,
            auto_illustration_requested=auto_illustration_requested,
            status=ConversationRunStatus.RUNNING.value,
            attempt_started_at=utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_draft_illustration(
        self, draft_id: str, asset_id: str, purpose: str, placement_after_paragraph: int,
        prompt: str = "", provider: str | None = None, model: str | None = None,
    ) -> DraftIllustrationRow:
        self.get_draft(draft_id)
        self.get_publication_asset(asset_id)
        if purpose == "cover":
            existing = self.session.scalar(
                select(DraftIllustrationRow).where(
                    DraftIllustrationRow.draft_id == draft_id,
                    DraftIllustrationRow.purpose == "cover",
                )
            )
            if existing:
                return existing
        row = DraftIllustrationRow(
            draft_id=draft_id, asset_id=asset_id, purpose=purpose,
            placement_after_paragraph=placement_after_paragraph, prompt=prompt,
            provider=provider, model=model,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list_draft_illustrations(self, draft_id: str) -> list[DraftIllustrationRow]:
        self.get_draft(draft_id)
        return list(self.session.scalars(
            select(DraftIllustrationRow).where(DraftIllustrationRow.draft_id == draft_id)
            .order_by(DraftIllustrationRow.purpose.asc(), DraftIllustrationRow.placement_after_paragraph.asc(), DraftIllustrationRow.created_at.asc())
        ))

    def remove_generated_draft_illustrations(self, draft_id: str) -> int:
        """替换自动配图时只解绑系统生成图，绝不删除人工上传图片或共享素材对象。"""
        rows = list(self.session.scalars(
            select(DraftIllustrationRow).where(
                DraftIllustrationRow.draft_id == draft_id,
                DraftIllustrationRow.provider.is_not(None),
            )
        ))
        for row in rows:
            self.session.delete(row)
        self.session.flush()
        return len(rows)

    def create_image_generation_job(
        self, chat_agent_run_id: str, draft_id: str, purpose: str, placement_after_paragraph: int,
        subject: str = "", style: str = "",
    ) -> ImageGenerationJobRow:
        if purpose not in {"cover", "inline"}:
            raise RepositoryError("图片任务类型仅支持封面或正文插图")
        if self.session.get(ChatAgentRunRow, chat_agent_run_id) is None:
            raise RepositoryError("对话 Agent 运行记录不存在")
        self.get_draft(draft_id)
        row = ImageGenerationJobRow(
            chat_agent_run_id=chat_agent_run_id,
            draft_id=draft_id,
            purpose=purpose,
            placement_after_paragraph=placement_after_paragraph,
            subject=(subject or "").strip() or None,
            style=(style or "").strip() or None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_image_generation_job(self, job_id: str) -> ImageGenerationJobRow:
        row = self.session.get(ImageGenerationJobRow, job_id)
        if row is None:
            raise RepositoryError("图片生成任务不存在")
        return row

    def list_image_generation_jobs(
        self, chat_agent_run_id: str, since: datetime | None = None,
    ) -> list[ImageGenerationJobRow]:
        statement = select(ImageGenerationJobRow).where(
            ImageGenerationJobRow.chat_agent_run_id == chat_agent_run_id
        )
        if since is not None:
            statement = statement.where(ImageGenerationJobRow.created_at >= since)
        return list(self.session.scalars(statement.order_by(ImageGenerationJobRow.created_at.asc())))

    def update_image_generation_job(
        self,
        job_id: str,
        status: str,
        *,
        arq_job_id: str | None = None,
        illustration_id: str | None = None,
        error_message: str | None = None,
    ) -> ImageGenerationJobRow:
        if status not in {"queued", "running", "completed", "failed", "timed_out"}:
            raise RepositoryError("图片生成任务状态不合法")
        row = self.get_image_generation_job(job_id)
        row.status = status
        if arq_job_id is not None:
            row.arq_job_id = arq_job_id
        if illustration_id is not None:
            row.illustration_id = illustration_id
        row.error_message = error_message
        if status == "running" and row.started_at is None:
            row.started_at = utcnow()
        if status in {"completed", "failed", "timed_out"}:
            row.finished_at = utcnow()
        self.session.flush()
        return row

    def delete_draft_illustration(self, draft_id: str, illustration_id: str) -> DraftIllustrationRow:
        row = self.session.get(DraftIllustrationRow, illustration_id)
        if row is None or row.draft_id != draft_id:
            raise RepositoryError("草稿插图不存在")
        self.session.delete(row)
        self.session.flush()
        return row

    def update_draft_illustration_position(
        self, draft_id: str, illustration_id: str, placement_after_paragraph: int
    ) -> DraftIllustrationRow:
        row = self.session.get(DraftIllustrationRow, illustration_id)
        if row is None or row.draft_id != draft_id:
            raise RepositoryError("草稿插图不存在")
        row.placement_after_paragraph = placement_after_paragraph
        self.session.flush()
        return row

    def update_draft_illustration(
        self, draft_id: str, illustration_id: str, purpose: str, placement_after_paragraph: int
    ) -> DraftIllustrationRow:
        """调整插图的用途（封面/正文）与段位；只改绑定关系，不动素材文件。"""
        if purpose not in {"cover", "inline"}:
            raise RepositoryError("插图用途仅支持封面或正文插图")
        row = self.session.get(DraftIllustrationRow, illustration_id)
        if row is None or row.draft_id != draft_id:
            raise RepositoryError("草稿插图不存在")
        row.purpose = purpose
        row.placement_after_paragraph = 0 if purpose == "cover" else placement_after_paragraph
        self.session.flush()
        return row

    def create_auto_review_run(
        self, draft_id: str, chat_agent_run_id: str | None, status: str = "running",
    ) -> AutoReviewRunRow:
        self.get_draft(draft_id)
        row = AutoReviewRunRow(draft_id=draft_id, chat_agent_run_id=chat_agent_run_id, status=status)
        self.session.add(row)
        self.session.flush()
        return row

    def get_auto_review_run(self, review_id: str) -> AutoReviewRunRow:
        row = self.session.get(AutoReviewRunRow, review_id)
        if row is None:
            raise RepositoryError("自动审核记录不存在")
        return row

    def find_active_auto_review_run(self, draft_id: str) -> AutoReviewRunRow | None:
        return self.session.scalar(
            select(AutoReviewRunRow)
            .where(
                AutoReviewRunRow.draft_id == draft_id,
                AutoReviewRunRow.status.in_(("queued", "running")),
            )
            .order_by(AutoReviewRunRow.created_at.desc())
        )

    def mark_auto_review_run_running(self, review_id: str) -> AutoReviewRunRow:
        row = self.get_auto_review_run(review_id)
        if row.status == "queued":
            row.status = "running"
            self.session.flush()
        return row

    def finish_auto_review_run(
        self, review_id: str, status: str, rule_report: dict, model_report: dict,
        error_message: str | None = None, wechat_job_id: str | None = None,
    ) -> AutoReviewRunRow:
        row = self.session.get(AutoReviewRunRow, review_id)
        if row is None:
            raise RepositoryError("自动审核记录不存在")
        row.status = status
        row.rule_report_json = rule_report
        row.model_report_json = model_report
        row.error_message = error_message
        row.wechat_job_id = wechat_job_id
        row.finished_at = utcnow()
        self.session.flush()
        return row

    def list_auto_review_runs(self, draft_id: str) -> list[AutoReviewRunRow]:
        return list(self.session.scalars(
            select(AutoReviewRunRow).where(AutoReviewRunRow.draft_id == draft_id)
            .order_by(AutoReviewRunRow.created_at.desc())
        ))

    def apply_auto_revision(
        self,
        draft_id: str,
        review_run_id: str,
        summary_cn: str,
        body: str,
        tags: list[str],
        revision_reason: dict,
        article_shape: dict[str, int] | None = None,
    ) -> DraftRow:
        """仅更新可改写文案字段，来源事实与原文标题尾注由服务端锁定。"""
        row = self.get_draft(draft_id)
        if row.status in {
            ReviewStatus.READY_TO_PUBLISH.value,
            ReviewStatus.DRAFTBOX_CREATED.value,
            ReviewStatus.PUBLISHED.value,
            ReviewStatus.DISCARDED.value,
            ReviewStatus.DELETED.value,
        }:
            raise InvalidReviewTransition("已定稿或已废弃草稿不能自动改写")
        from app.services.plain_text import format_source_body, normalize_wechat_description

        row.summary_cn = normalize_wechat_description(summary_cn)
        row.body = format_source_body(body, row.source_item.title, row.source_item.source_kind)
        row.tags_json = [str(tag).strip("# ") for tag in tags if str(tag).strip("# ")][:10]
        if article_shape:
            row.content_plan_json = {
                **(row.content_plan_json or {}),
                "article_shape": article_shape,
            }
            row.content_plan_json.pop("logical_sections", None)
        row.version += 1
        row.status = ReviewStatus.PENDING_REVIEW.value
        snapshot = DraftRevisionRow(
            draft_id=row.id,
            auto_review_run_id=review_run_id,
            version=row.version,
            summary_cn=row.summary_cn,
            body=row.body,
            tags_json=row.tags_json,
            revision_reason_json=revision_reason,
        )
        self.session.add(snapshot)
        self.session.flush()
        return row

    def list_draft_revisions(self, draft_id: str) -> list[DraftRevisionRow]:
        self.get_draft(draft_id)
        return list(self.session.scalars(
            select(DraftRevisionRow)
            .where(DraftRevisionRow.draft_id == draft_id)
            .order_by(DraftRevisionRow.version.desc())
        ))

    def add_chat_agent_event(
        self,
        run_id: str,
        title: str,
        detail: str = "",
        status: str = "completed",
        metadata: dict | None = None,
    ) -> ChatAgentEventRow:
        # 锁定父运行记录，保证 Web 请求和后台 Worker 写同一运行记录时序号连续。
        run = self.session.scalar(
            select(ChatAgentRunRow)
            .where(ChatAgentRunRow.id == run_id)
            .with_for_update()
        )
        if run is None:
            raise RepositoryError(f"对话 Agent 运行记录不存在：{run_id}")
        sequence = self.session.scalar(
            select(func.coalesce(func.max(ChatAgentEventRow.sequence) + 1, 1)).where(
                ChatAgentEventRow.run_id == run_id
            )
        )
        row = ChatAgentEventRow(
            run_id=run_id,
            sequence=sequence,
            title=title,
            detail=detail,
            status=status,
            metadata_json=metadata or {},
        )
        self.session.add(row)
        self.session.flush()
        return row

    def finish_chat_agent_run(
        self,
        run_id: str,
        response_message_id: str | None,
        status: ConversationRunStatus | str,
        summary: str,
        tool_results: list[dict] | None = None,
        error_message: str | None = None,
    ) -> ChatAgentRunRow:
        row = self.session.get(ChatAgentRunRow, run_id)
        if row is None:
            raise RepositoryError(f"对话 Agent 运行记录不存在：{run_id}")
        row.response_message_id = response_message_id
        # 与 create_chat_agent_run 一致：枚举与字符串都接受，避免调用方写字符串就崩。
        row.status = status.value if isinstance(status, ConversationRunStatus) else str(status)
        row.summary = summary
        row.tool_results_json = tool_results or []
        row.error_message = error_message
        row.finished_at = utcnow()
        self.session.flush()
        return row

    def get_chat_agent_run(self, run_id: str) -> ChatAgentRunRow:
        row = self.session.get(ChatAgentRunRow, run_id)
        if row is None:
            raise RepositoryError(f"对话 Agent 运行记录不存在：{run_id}")
        return row

    def list_chat_agent_runs(self, session_id: str) -> list[ChatAgentRunRow]:
        self.get_chat_session(session_id)
        return list(
            self.session.scalars(
                select(ChatAgentRunRow)
                .where(ChatAgentRunRow.session_id == session_id)
                .order_by(ChatAgentRunRow.created_at.asc())
            )
        )

    def list_active_chat_agent_runs(self, limit: int = 50) -> list[ChatAgentRunRow]:
        return list(
            self.session.scalars(
                select(ChatAgentRunRow)
                .where(ChatAgentRunRow.status == ConversationRunStatus.RUNNING.value)
                .order_by(ChatAgentRunRow.created_at.desc())
                .limit(limit)
            )
        )

    def reconcile_stale_generation_runs(
        self, stale_after_seconds: int
    ) -> list[ChatAgentRunRow]:
        """安全结束已超过全部后台任务时限的生成记录，不删除其草稿和审计。"""
        cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
        rows = list(
            self.session.scalars(
                select(ChatAgentRunRow)
                .where(
                    ChatAgentRunRow.intent.in_(GENERATION_RECORD_INTENTS),
                    ChatAgentRunRow.status == ConversationRunStatus.RUNNING.value,
                    ChatAgentRunRow.attempt_started_at <= cutoff,
                )
                .with_for_update()
            )
        )
        if not rows:
            return []
        detail = "后台任务超过安全等待时限仍未结束，已停止等待；已有草稿和图片均已保留。"
        for row in rows:
            row.status = ConversationRunStatus.FAILED.value
            row.summary = "生成任务未完成"
            row.error_message = detail
            row.finished_at = utcnow()
            if row.response_message_id:
                self.update_chat_message(row.response_message_id, detail)
            self.add_chat_agent_event(
                row.id,
                "遗留生成任务已收束",
                detail,
                "failed",
                metadata={"phase": "finalization", "state": "stale_failed"},
            )
            self.upsert_failure_notification(
                "chat_agent_run",
                row.id,
                "generation",
                "生成任务超时未完成",
                detail,
                "review",
            )
        self.session.flush()
        return rows

    def delete_generation_run_audit(self, run_id: str) -> str:
        """仅删除已结束生成任务的运行审计，保留草稿、插图和会话数据。

        生成记录队列里出现的运行（生成、附件成稿、配图、审核、投递、审核决定）都应可删除，
        否则前端对这些条目点删除会直接报错。
        """
        row = self.get_chat_agent_run(run_id)
        if row.intent not in GENERATION_RECORD_INTENTS:
            raise RepositoryError("仅能删除生成队列中的任务记录")
        if row.status == ConversationRunStatus.RUNNING.value:
            raise RepositoryError("运行中的任务不能删除，请等待完成或超时收束")
        for notification in self.session.scalars(
            select(NotificationRow).where(
                NotificationRow.source_type == "chat_agent_run",
                NotificationRow.source_id == run_id,
            )
        ):
            self.session.delete(notification)
        self.session.delete(row)
        self.session.flush()
        return run_id

    def list_generation_chat_agent_runs(self, limit: int = 100) -> list[ChatAgentRunRow]:
        """生成记录页使用的终态/运行态任务列表，不混入普通对话和计划确认。

        生成、审核、配图、审核决定都是“任务”，都要在队列里可见；只有纯对话与计划确认不进列表。
        """
        return list(
            self.session.scalars(
                select(ChatAgentRunRow)
                .where(ChatAgentRunRow.intent.in_(GENERATION_RECORD_INTENTS))
                .order_by(ChatAgentRunRow.created_at.desc())
                .limit(limit)
            )
        )

    def find_generation_run_for_draft(self, draft_id: str) -> ChatAgentRunRow | None:
        """从文字阶段审计找回原生成记录，兼容既有数据。"""
        return self.session.scalar(
            select(ChatAgentRunRow)
            .join(ChatAgentEventRow, ChatAgentEventRow.run_id == ChatAgentRunRow.id)
            .where(
                ChatAgentRunRow.intent == ConversationIntent.COLLECT_NEWS.value,
                ChatAgentEventRow.metadata_json["draft_ids"].contains([draft_id]),
            )
            .order_by(ChatAgentRunRow.created_at.desc())
        )

    def reopen_generation_run(
        self, run: ChatAgentRunRow, response_message_id: str,
        auto_review_requested: bool, auto_illustration_requested: bool,
    ) -> ChatAgentRunRow:
        run.status = ConversationRunStatus.RUNNING.value
        run.summary = "正在原记录内重新生成"
        run.error_message = None
        run.finished_at = None
        run.attempt_started_at = utcnow()
        run.response_message_id = response_message_id
        run.auto_review_requested = run.auto_review_requested or auto_review_requested
        run.auto_illustration_requested = run.auto_illustration_requested or auto_illustration_requested
        self.session.flush()
        return run

    def upsert_failure_notification(
        self,
        source_type: str,
        source_id: str,
        category: str,
        title: str,
        detail: str,
        target_view: str,
    ) -> NotificationRow:
        """为同一失败来源保留一条通知；错误内容变化时重新标记为未读。"""
        safe_detail = detail.strip() or "任务执行失败，请在对应页面查看记录。"
        fingerprint = sha256(f"{category}|{title}|{safe_detail}".encode("utf-8")).hexdigest()
        row = self.session.scalar(
            select(NotificationRow).where(
                NotificationRow.source_type == source_type,
                NotificationRow.source_id == source_id,
            )
        )
        if row is None:
            row = NotificationRow(
                source_type=source_type,
                source_id=source_id,
                category=category,
                title=title,
                detail=safe_detail,
                target_view=target_view,
                fingerprint=fingerprint,
            )
            self.session.add(row)
        elif row.fingerprint != fingerprint:
            row.category = category
            row.title = title
            row.detail = safe_detail
            row.target_view = target_view
            row.fingerprint = fingerprint
            row.is_read = False
            row.read_at = None
            row.dismissed_at = None
        self.session.flush()
        return row

    def sync_failure_notifications(self) -> int:
        """将已有失败审计补齐为通知，兼容通知中心上线前的历史记录。"""
        before = len(self.list_notifications(include_dismissed=True, limit=500))
        for row in self.session.scalars(
            select(ChatAgentRunRow).where(ChatAgentRunRow.status == ConversationRunStatus.FAILED.value)
        ):
            self.upsert_failure_notification(
                "chat_agent_run", row.id, "generation", "文案生成失败",
                row.error_message or row.summary or "生成任务失败", "review",
            )
        for row in self.session.scalars(
            select(ImageGenerationJobRow).where(ImageGenerationJobRow.status.in_(("failed", "timed_out")))
        ):
            title = "图片生成超时" if row.status == "timed_out" else "图片生成失败"
            self.upsert_failure_notification(
                "image_generation_job", row.id, "image", title,
                row.error_message or "图片任务未生成，文字草稿已保留。", "review",
            )
        for row in self.session.scalars(
            select(WechatPublicationJobRow).where(
                # 只有**真失败**状态才算投递失败。
                # `delivery_stale`／`superseded` 等状态也会带说明文本（“配图已调整，旧选择失效”），
                # 那不是错误，此前被这条同步逻辑当成“公众号投递失败”反复报出来。
                WechatPublicationJobRow.state.in_(("draft_failed", "delivery_failed")),
            )
        ):
            self.upsert_failure_notification(
                "wechat_publication", row.id, "wechat", "公众号投递失败",
                row.error_message or "公众号投递未完成，请检查发布情况。", "publishing",
            )
        after = len(self.list_notifications(include_dismissed=True, limit=500))
        return max(0, after - before)

    def list_notifications(
        self, *, include_dismissed: bool = False, limit: int = 100,
    ) -> list[NotificationRow]:
        statement = select(NotificationRow).order_by(NotificationRow.updated_at.desc()).limit(limit)
        if not include_dismissed:
            statement = statement.where(NotificationRow.dismissed_at.is_(None))
        return list(self.session.scalars(statement))

    def unread_notification_count(self) -> int:
        return int(self.session.scalar(
            select(func.count(NotificationRow.id)).where(
                NotificationRow.dismissed_at.is_(None),
                NotificationRow.is_read.is_(False),
            )
        ) or 0)

    def mark_notification_read(self, notification_id: str) -> NotificationRow:
        row = self.session.get(NotificationRow, notification_id)
        if row is None or row.dismissed_at is not None:
            raise RepositoryError("通知不存在")
        row.is_read = True
        row.read_at = row.read_at or utcnow()
        self.session.flush()
        return row

    def mark_all_notifications_read(self) -> int:
        rows = self.session.scalars(
            select(NotificationRow).where(
                NotificationRow.dismissed_at.is_(None), NotificationRow.is_read.is_(False),
            )
        )
        count = 0
        for row in rows:
            row.is_read = True
            row.read_at = utcnow()
            count += 1
        self.session.flush()
        return count

    def dismiss_notification(self, notification_id: str) -> NotificationRow:
        row = self.session.get(NotificationRow, notification_id)
        if row is None or row.dismissed_at is not None:
            raise RepositoryError("通知不存在")
        row.dismissed_at = utcnow()
        self.session.flush()
        return row

    def list_chat_agent_events(self, run_id: str) -> list[ChatAgentEventRow]:
        return list(
            self.session.scalars(
                select(ChatAgentEventRow)
                .where(ChatAgentEventRow.run_id == run_id)
                .order_by(ChatAgentEventRow.sequence.asc())
            )
        )

    def create_schedule_plan(
        self,
        session_id: str,
        run_id: str,
        schedule_text: str,
        task_summary: str,
        sources: list[str],
    ) -> SchedulePlanRow:
        row = SchedulePlanRow(
            session_id=session_id,
            chat_agent_run_id=run_id,
            schedule_text=schedule_text,
            task_summary=task_summary,
            sources_json=sources,
            status=PlanStatus.PENDING_CONFIRMATION.value,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_publish_plan(
        self,
        session_id: str,
        run_id: str,
        platform: str,
        request_summary: str,
        draft_id: str | None = None,
    ) -> PublishPlanRow:
        row = PublishPlanRow(
            session_id=session_id,
            chat_agent_run_id=run_id,
            draft_id=draft_id,
            platform=platform,
            request_summary=request_summary,
            status=PlanStatus.PENDING_CONFIRMATION.value,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def confirm_schedule_plan(
        self, plan_id: str, idempotency_key: str
    ) -> SchedulePlanRow:
        row = self.session.get(SchedulePlanRow, plan_id)
        if row is None:
            raise RepositoryError(f"定时计划不存在：{plan_id}")
        if row.status == PlanStatus.CONFIRMED.value:
            if row.confirmation_key == idempotency_key:
                return row
            raise InvalidReviewTransition("定时计划已被其他确认请求处理")
        if row.confirmation_key and row.confirmation_key != idempotency_key:
            raise InvalidReviewTransition("定时计划已被其他确认请求处理")
        if row.status == PlanStatus.CANCELLED.value:
            raise InvalidReviewTransition("已取消的定时计划不能确认")
        row.status = PlanStatus.CONFIRMED.value
        row.confirmation_key = idempotency_key
        row.confirmed_at = utcnow()
        self._mark_chat_plan_confirmed(
            row.chat_agent_run_id, plan_id, "create_schedule_plan"
        )
        self.session.flush()
        return row

    def confirm_publish_plan(
        self, plan_id: str, idempotency_key: str
    ) -> PublishPlanRow:
        row = self.session.get(PublishPlanRow, plan_id)
        if row is None:
            raise RepositoryError(f"发布计划不存在：{plan_id}")
        if row.status == PlanStatus.CONFIRMED.value:
            if row.confirmation_key == idempotency_key:
                return row
            raise InvalidReviewTransition("发布计划已被其他确认请求处理")
        if row.confirmation_key and row.confirmation_key != idempotency_key:
            raise InvalidReviewTransition("发布计划已被其他确认请求处理")
        if row.status == PlanStatus.CANCELLED.value:
            raise InvalidReviewTransition("已取消的发布计划不能确认")
        row.status = PlanStatus.CONFIRMED.value
        row.confirmation_key = idempotency_key
        row.confirmed_at = utcnow()
        self._mark_chat_plan_confirmed(
            row.chat_agent_run_id, plan_id, "create_publish_plan"
        )
        self.session.flush()
        return row

    def _mark_chat_plan_confirmed(
        self, run_id: str, plan_id: str, tool_name: str
    ) -> None:
        """同步会话审计状态，避免刷新页面后再次出现已确认计划的确认按钮。"""
        run = self.session.get(ChatAgentRunRow, run_id)
        if run is None:
            return
        run.tool_results_json = [
            {
                **result,
                **({"status": PlanStatus.CONFIRMED.value}
                   if result.get("tool") == tool_name and result.get("plan_id") == plan_id
                   else {}),
            }
            for result in run.tool_results_json
        ]
        run.status = ConversationRunStatus.COMPLETED.value
        run.summary = "计划已确认（当前版本仅记录确认，不会执行）"
        self.add_chat_agent_event(
            run_id,
            "确认计划",
            "已记录确认；当前版本未注册定时任务，也未向任何平台发布。",
        )

    def finish_attachment_processing(
        self,
        processing_id: str,
        status: AttachmentStatus,
        draft_id: str | None = None,
        error_message: str | None = None,
    ) -> AttachmentProcessingRow:
        row = self.session.get(AttachmentProcessingRow, processing_id)
        if row is None:
            raise RepositoryError(f"附件处理记录不存在：{processing_id}")
        row.status = status.value
        row.draft_id = draft_id
        row.error_message = error_message
        row.finished_at = utcnow()
        attachment = self.get_attachment(row.attachment_id)
        attachment.status = status.value
        self.session.flush()
        return row

    def get_source_by_external_id(
        self, source_kind: str, external_id: str
    ) -> SourceItemRow | None:
        return self.session.scalar(
            select(SourceItemRow).where(
                SourceItemRow.source_kind == source_kind,
                SourceItemRow.external_id == external_id,
            )
        )

    def get_draft_for_source(self, source_item_id: str) -> DraftRow | None:
        return self.session.scalar(
            select(DraftRow)
            .where(DraftRow.source_item_id == source_item_id)
            .order_by(DraftRow.created_at.desc())
            .limit(1)
        )

    def get_deduplicating_draft_for_source(self, source_item_id: str) -> DraftRow | None:
        """仅已发布或仍有效的草稿阻止相同来源内容再次生成。"""
        source = self.session.get(SourceItemRow, source_item_id)
        if source is None:
            return None
        return self.session.scalar(
            select(DraftRow)
            .join(SourceItemRow, DraftRow.source_item_id == SourceItemRow.id)
            .where(
                SourceItemRow.content_hash == source.content_hash,
                DraftRow.status.not_in(
                    [ReviewStatus.DISCARDED.value, ReviewStatus.DELETED.value]
                ),
            )
            .order_by(DraftRow.created_at.desc())
            .limit(1)
        )

    def introduced_external_ids(self, source_kind: str, external_ids: list[str]) -> set[str]:
        """公众号草稿箱已创建或历史真实发布的来源均会在下一轮候选中被排除。"""
        if not external_ids:
            return set()
        statement = (
            select(SourceItemRow.external_id)
            .outerjoin(ProjectIntroductionRow, ProjectIntroductionRow.source_item_id == SourceItemRow.id)
            .where(
                SourceItemRow.source_kind == source_kind,
                SourceItemRow.external_id.in_(external_ids),
                ProjectIntroductionRow.id.is_not(None),
            )
        )
        return set(self.session.scalars(statement))

    def deduplicating_github_candidate_external_ids(self, external_ids: list[str]) -> set[str]:
        """在 Trending 排序前排除已入草稿箱来源和已有有效草稿的同仓库候选。"""
        if not external_ids:
            return set()
        statement = (
            select(SourceItemRow.external_id)
            .outerjoin(
                ProjectIntroductionRow,
                ProjectIntroductionRow.source_item_id == SourceItemRow.id,
            )
            .outerjoin(DraftRow, DraftRow.source_item_id == SourceItemRow.id)
            .where(
                SourceItemRow.source_kind == "github",
                SourceItemRow.external_id.in_(external_ids),
                or_(
                    ProjectIntroductionRow.id.is_not(None),
                    and_(
                        DraftRow.id.is_not(None),
                        DraftRow.status.not_in(
                            [ReviewStatus.DISCARDED.value, ReviewStatus.DELETED.value]
                        ),
                    ),
                ),
            )
        )
        return set(self.session.scalars(statement))

    def record_project_introduction(
        self, source: SourceItemRow, draft: DraftRow, publication: PublicationRecordRow
    ) -> ProjectIntroductionRow:
        existing = self.session.scalar(
            select(ProjectIntroductionRow).where(
                ProjectIntroductionRow.source_kind == source.source_kind,
                ProjectIntroductionRow.external_id == source.external_id,
            )
        )
        if existing:
            existing.draft_id = draft.id
            existing.publication_id = publication.id
            existing.introduced_at = publication.published_at
            self.session.flush()
            return existing
        row = ProjectIntroductionRow(
            source_kind=source.source_kind,
            external_id=source.external_id,
            source_item_id=source.id,
            draft_id=draft.id,
            publication_id=publication.id,
            introduced_at=publication.published_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_draftbox_introduction(
        self, source: SourceItemRow, draft: DraftRow,
    ) -> ProjectIntroductionRow:
        """草稿箱创建成功即写入四类来源去重，不伪造真实发布记录。"""
        existing = self.session.scalar(
            select(ProjectIntroductionRow).where(
                ProjectIntroductionRow.source_kind == source.source_kind,
                ProjectIntroductionRow.external_id == source.external_id,
            )
        )
        if existing:
            if existing.draft_id != draft.id:
                raise InvalidReviewTransition("该来源已在公众号草稿箱中，不能重复写入去重记录")
            return existing
        row = ProjectIntroductionRow(
            source_kind=source.source_kind,
            external_id=source.external_id,
            source_item_id=source.id,
            draft_id=draft.id,
            publication_id=None,
            introduced_at=utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_manual_publication(
        self, draft_id: str, command: ManualPublicationCommand
    ) -> DraftRow:
        previous = self.session.scalar(
            select(PublicationRecordRow).where(
                PublicationRecordRow.idempotency_key == command.idempotency_key
            )
        )
        if previous:
            if previous.draft_id != draft_id:
                raise InvalidReviewTransition("该幂等键已用于其他草稿的发布回填")
            return self.get_draft(draft_id)

        draft = self.get_draft(draft_id)
        if draft.status != ReviewStatus.READY_TO_PUBLISH.value:
            raise InvalidReviewTransition("只有审核通过且尚未发布的草稿可以回填发布链接")

        publication = PublicationRecordRow(
            draft_id=draft.id,
            operator=command.operator,
            platform=command.platform,
            published_url=str(command.published_url),
            note=command.note,
            idempotency_key=command.idempotency_key,
        )
        self.session.add(publication)
        self.session.flush()
        draft.status = ReviewStatus.PUBLISHED.value
        draft.published_platform = command.platform
        draft.published_url = str(command.published_url)
        draft.published_at = publication.published_at
        if draft.source_item.source_kind == "github":
            self.record_project_introduction(draft.source_item, draft, publication)
        self.session.flush()
        return draft

    def get_wechat_publication(self, job_id: str) -> WechatPublicationJobRow:
        """按投递尝试 ID 读取任务，而不是按内容文案读取。"""
        row = self.session.get(WechatPublicationJobRow, job_id)
        if row is None:
            raise WechatPublicationNotFound("该公众号投递任务不存在")
        return row

    def get_wechat_publication_for_draft(self, draft_id: str) -> WechatPublicationJobRow | None:
        return self.session.scalar(
            select(WechatPublicationJobRow).where(WechatPublicationJobRow.draft_id == draft_id)
        )

    def list_wechat_publications(self, limit: int = 100) -> list[WechatPublicationJobRow]:
        return list(
            self.session.scalars(
                select(WechatPublicationJobRow)
                .order_by(WechatPublicationJobRow.updated_at.desc())
                .limit(limit)
            )
        )

    def save_wechat_publication(
        self,
        draft_id: str,
        cover_asset_id: str,
        cover_media_id: str,
        inline_asset_ids: list[str],
        inline_image_urls: list[dict[str, object]],
    ) -> WechatPublicationJobRow:
        draft = self.get_draft(draft_id)
        # 已投递的草稿也允许刷新投递素材：文案或配图改过之后要能覆盖远端草稿内容。
        if draft.status not in {
            ReviewStatus.READY_TO_PUBLISH.value,
            ReviewStatus.DRAFTBOX_CREATED.value,
        }:
            raise InvalidReviewTransition("只有审核通过的文章可以创建或更新公众号草稿")
        # 文案重生成会先让未成功的旧投递素材失效；此处复用同一审计记录，
        # 避免 unique(draft_id) 使新图片无法进入后续投递。
        row = self.get_wechat_publication_for_draft(draft_id)
        if row is None:
            row = WechatPublicationJobRow(draft_id=draft_id)
            self.session.add(row)
        # 远端草稿已存在时保留 media_id：重新投递走 draft/update 原地覆盖，而不是新建。
        refreshing_existing_draft = bool(row.wechat_draft_media_id)
        row.cover_asset_id = cover_asset_id
        row.cover_media_id = cover_media_id
        row.inline_asset_ids_json = inline_asset_ids
        row.inline_image_urls_json = inline_image_urls
        if not refreshing_existing_draft:
            row.wechat_draft_media_id = None
        row.state = "cover_uploaded"
        row.error_message = None
        self.session.flush()
        return row

    def save_wechat_asset_selection(
        self,
        draft_id: str,
        cover_asset_id: str,
        inline_asset_ids: list[str],
    ) -> WechatPublicationJobRow:
        """在审核前固化 Agent 已决定的投递图片，供审核后的投递和重试复用。"""
        self.get_draft(draft_id)
        self.get_publication_asset(cover_asset_id)
        for asset_id in inline_asset_ids:
            self.get_publication_asset(asset_id)
        row = self.get_wechat_publication_for_draft(draft_id)
        if row is None:
            row = WechatPublicationJobRow(draft_id=draft_id)
            self.session.add(row)
        if row.wechat_draft_media_id and row.state != "delivery_stale":
            # 已投递且未标记过期：不接受新的图片选择（历史不变式）。
            # 标记为 delivery_stale 时说明用户在改已投递的草稿，必须允许重新选择。
            return row
        row.cover_asset_id = cover_asset_id
        row.cover_media_id = None
        row.inline_asset_ids_json = list(inline_asset_ids)
        row.inline_image_urls_json = []
        row.state = "assets_selected"
        row.error_message = None
        self.session.flush()
        return row

    def mark_wechat_publication_stale(self, draft_id: str, reason: str) -> bool:
        """文案或配图在投递后又被修改：标记为“投递已过期”，下次投递原地覆盖远端草稿。

        远端草稿不会被删除，media_id 保留，重新投递时用 `draft/update` 覆盖内容。
        """
        row = self.get_wechat_publication_for_draft(draft_id)
        if row is None or not row.wechat_draft_media_id:
            return False
        row.state = "delivery_stale"
        row.error_message = reason
        # 图片可能已变：清空旧选择，重新投递时按当前图片重新确定并重新上传。
        row.cover_asset_id = None
        row.cover_media_id = None
        row.inline_asset_ids_json = []
        row.inline_image_urls_json = []
        self.session.flush()
        return True

    def invalidate_unfinished_wechat_publication_for_regeneration(
        self,
        draft_id: str,
        reason: str = "文案已重新生成，旧投递素材已失效；下次投递将重新选择当前图片。",
    ) -> bool:
        """文案或配图变更后不得复用旧选择。

        尚未投递时清空选择；**已经投递过时标记为过期**，由下次投递覆盖远端草稿内容，
        而不是让远端草稿停留在一个已经过时的快照上。
        """
        row = self.get_wechat_publication_for_draft(draft_id)
        if row is None:
            return False
        if row.wechat_draft_media_id:
            return self.mark_wechat_publication_stale(draft_id, reason)
        row.cover_asset_id = None
        row.cover_media_id = None
        row.inline_asset_ids_json = []
        row.inline_image_urls_json = []
        row.state = "superseded"
        row.error_message = reason
        self.session.flush()
        return True

    def mark_wechat_draft_created(
        self, job_id: str, wechat_draft_media_id: str
    ) -> WechatPublicationJobRow:
        row = self.get_wechat_publication(job_id)
        row.wechat_draft_media_id = wechat_draft_media_id
        row.state = "draft_created"
        row.error_message = None
        draft = self.get_draft(row.draft_id)
        draft.status = ReviewStatus.DRAFTBOX_CREATED.value
        self.record_draftbox_introduction(draft.source_item, draft)
        self.session.flush()
        return row

    def mark_wechat_draft_updated(self, job_id: str) -> WechatPublicationJobRow:
        """远端草稿已按当前文案与配图原地覆盖：回到已创建状态并保留 media_id。"""
        row = self.get_wechat_publication(job_id)
        row.state = "draft_created"
        row.error_message = None
        draft = self.get_draft(row.draft_id)
        draft.status = ReviewStatus.DRAFTBOX_CREATED.value
        self.session.flush()
        return row

    def mark_wechat_submitted(
        self, job_id: str, publish_id: str, idempotency_key: str
    ) -> WechatPublicationJobRow:
        row = self.get_wechat_publication(job_id)
        if row.submit_idempotency_key and row.submit_idempotency_key != idempotency_key:
            raise InvalidReviewTransition("该公众号草稿已经提交，不能重复提交")
        row.wechat_publish_id = publish_id
        row.submit_idempotency_key = idempotency_key
        row.state = "publishing"
        row.error_message = None
        self.session.flush()
        return row

    def mark_wechat_status(
        self, job_id: str, state: str, published_url: str | None = None,
        error_message: str | None = None,
    ) -> WechatPublicationJobRow:
        row = self.get_wechat_publication(job_id)
        row.state = state
        row.published_url = published_url or row.published_url
        row.error_message = error_message
        self.session.flush()
        return row

    def finish_agent_run(
        self,
        run_id: str,
        status: AgentRunStatus,
        tool_results: list[dict],
        error_message: str | None = None,
    ) -> AgentRunRow:
        row = self.session.get(AgentRunRow, run_id)
        if row is None:
            raise RepositoryError(f"主 Agent 执行记录不存在：{run_id}")
        row.status = status.value
        row.tool_results_json = tool_results
        row.error_message = error_message
        row.finished_at = utcnow()
        self.session.flush()
        return row

    def list_agent_runs(self, limit: int = 100) -> list[AgentRunRow]:
        return list(
            self.session.scalars(
                select(AgentRunRow).order_by(AgentRunRow.started_at.desc()).limit(limit)
            )
        )

    def list_collection_runs(self, limit: int = 100) -> list[CollectionRunRow]:
        return list(
            self.session.scalars(
                select(CollectionRunRow)
                .order_by(CollectionRunRow.started_at.desc())
                .limit(limit)
            )
        )

    def save_trending_snapshots(
        self,
        run_id: str,
        items: list[NormalizedItem],
        captured_at: datetime,
    ) -> int:
        count = 0
        for item in items:
            if item.source_kind.value != "github":
                continue
            source = self.session.scalar(
                select(SourceItemRow).where(
                    SourceItemRow.source_kind == item.source_kind.value,
                    SourceItemRow.external_id == item.external_id,
                )
            )
            if source is None:
                continue
            for period, rank in item.metadata.get("periods", {}).items():
                period_metrics = item.metadata.get("period_metrics", {}).get(period, {})
                self.session.add(
                    TrendingSnapshotRow(
                        collection_run_id=run_id,
                        source_item_id=source.id,
                        period=period,
                        rank=int(rank),
                        stars_period=int(
                            period_metrics.get(
                                "stars_period", item.metrics.get("stars_period", 0)
                            )
                        ),
                        stars_total=int(item.metrics.get("stars_total", 0)),
                        captured_at=captured_at,
                    )
                )
                count += 1
        self.session.flush()
        return count

    def list_trending_snapshots(
        self, source_item_id: str, limit: int = 100
    ) -> list[TrendingSnapshotRow]:
        return list(
            self.session.scalars(
                select(TrendingSnapshotRow)
                .where(TrendingSnapshotRow.source_item_id == source_item_id)
                .order_by(TrendingSnapshotRow.captured_at.desc())
                .limit(limit)
            )
        )

    def create_draft(
        self, source: SourceItemRow, content: DraftContent, evidence: list[dict] | None = None
    ) -> DraftRow:
        existing = self.get_deduplicating_draft_for_source(source.id)
        if existing:
            return existing
        row = DraftRow(
            source_item_id=source.id,
            title_options_json=content.title_options,
            summary_cn=content.summary_cn,
            body=content.body,
            tags_json=content.tags,
            card_script_json=content.card_script,
            source_name=content.source_name,
            source_url=str(content.source_url),
            evidence_json=evidence or [],
            content_plan_json=content.content_plan,
            quality_report_json=content.quality_report,
            claim_citations_json=content.claim_citations,
            generation_mode=content.generation_mode,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_active_draft_source_snapshot(self, draft_id: str) -> DraftSourceSnapshotRow | None:
        return self.session.scalar(
            select(DraftSourceSnapshotRow).where(
                DraftSourceSnapshotRow.draft_id == draft_id,
                DraftSourceSnapshotRow.object_key.is_not(None),
                DraftSourceSnapshotRow.content_deleted_at.is_(None),
            )
        )

    def save_draft_source_snapshot(
        self, draft_id: str, source_item_id: str, source_url: str, content_origin: str,
        object_key: str, sha256: str, content_length: int,
    ) -> DraftSourceSnapshotRow:
        existing = self.session.scalar(
            select(DraftSourceSnapshotRow).where(DraftSourceSnapshotRow.draft_id == draft_id)
        )
        if existing:
            return existing
        row = DraftSourceSnapshotRow(
            draft_id=draft_id,
            source_item_id=source_item_id,
            source_url=source_url,
            content_origin=content_origin,
            object_key=object_key,
            sha256=sha256,
            content_length=content_length,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def mark_draft_source_snapshot_deleted(self, draft_id: str) -> DraftSourceSnapshotRow | None:
        row = self.get_active_draft_source_snapshot(draft_id)
        if row is None:
            return None
        row.object_key = None
        row.content_deleted_at = utcnow()
        self.session.flush()
        return row

    def regenerate_draft(
        self, draft_id: str, content: DraftContent, evidence: list[dict] | None = None,
    ) -> DraftRow:
        """在原草稿内保存一版重生成文案，保留来源、图片、审核和发布审计。"""
        row = self.get_draft(draft_id)
        if row.status in {
            ReviewStatus.PUBLISHED.value,
            ReviewStatus.DISCARDED.value,
            ReviewStatus.DELETED.value,
        }:
            raise InvalidReviewTransition("已发布或已废弃草稿不能覆盖重生成")
        from app.services.plain_text import format_source_body, normalize_wechat_description

        row.title_options_json = content.title_options
        row.summary_cn = normalize_wechat_description(content.summary_cn)
        row.body = format_source_body(content.body, row.source_item.title, row.source_item.source_kind)
        row.tags_json = [str(tag).strip("# ") for tag in content.tags if str(tag).strip("# ")][:10]
        row.card_script_json = content.card_script
        row.evidence_json = evidence or row.evidence_json
        row.content_plan_json = content.content_plan
        row.quality_report_json = content.quality_report
        row.claim_citations_json = content.claim_citations
        row.generation_mode = content.generation_mode
        row.version += 1
        row.status = ReviewStatus.PENDING_REVIEW.value
        self.session.add(DraftRevisionRow(
            draft_id=row.id,
            auto_review_run_id=None,
            version=row.version,
            summary_cn=row.summary_cn,
            body=row.body,
            tags_json=row.tags_json,
            revision_reason_json={"kind": "source_regeneration"},
        ))
        self.session.flush()
        return row

    def list_sources(self, limit: int = 100) -> list[SourceItemRow]:
        return list(
            self.session.scalars(
                select(SourceItemRow)
                .order_by(SourceItemRow.hot_score.desc(), SourceItemRow.created_at.desc())
                .limit(limit)
            )
        )

    def list_drafts(
        self,
        status: str | None = None,
        limit: int = 100,
        created_from: datetime | None = None,
        created_until: datetime | None = None,
        include_deleted: bool = False,
    ) -> list[DraftRow]:
        statement = select(DraftRow)
        if status:
            statement = statement.where(DraftRow.status == status)
        if not include_deleted:
            statement = statement.where(DraftRow.status != ReviewStatus.DELETED.value)
        if created_from:
            statement = statement.where(DraftRow.created_at >= created_from)
        if created_until:
            statement = statement.where(DraftRow.created_at < created_until)
        statement = statement.order_by(DraftRow.created_at.desc()).limit(limit)
        return list(self.session.scalars(statement))

    def get_draft(self, draft_id: str) -> DraftRow:
        row = self.session.get(DraftRow, draft_id)
        if row is None:
            raise DraftNotFound(f"草稿不存在：{draft_id}")
        return row

    def append_draft_evidence(self, draft_id: str, entries: list[dict]) -> int:
        """把补充资料（例如改稿时的联网检索结果）并入草稿证据。

        审核与改稿都从 `evidence_json` 取证据：只写进改稿提示词而不落库，
        审核模型就看不到这些事实，只能把它们判成“来源证据中未出现”。
        """
        if not entries:
            return 0
        row = self.get_draft(draft_id)
        existing = list(row.evidence_json or [])
        known = {str(item.get("id")) for item in existing if isinstance(item, dict)}
        prefix = "search"
        appended = 0
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("content") or "").strip():
                continue
            index = len([item for item in existing if str(item.get("id", "")).startswith(prefix)]) + 1
            candidate = f"{prefix}-{index}"
            while candidate in known:
                index += 1
                candidate = f"{prefix}-{index}"
            known.add(candidate)
            existing.append({**entry, "id": candidate, "origin": "revision_search"})
            appended += 1
        if appended:
            row.evidence_json = existing
            self.session.flush()
        return appended

    def expire_stale_auto_review_run(self, draft_id: str, timeout_seconds: int) -> None:
        """清理僵死的审核任务：服务重启或超时中断会让记录永远停在 running，并挡住新的审核。"""
        row = self.find_active_auto_review_run(draft_id)
        if row is None:
            return
        created_at = row.created_at
        if created_at is None or created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC) if created_at else utcnow()
        if (utcnow() - created_at).total_seconds() < max(timeout_seconds, 60):
            return
        self.finish_auto_review_run(
            row.id, "failed", {}, {}, "上次自动审核任务已中断（超时或服务重启），已自动清理，可重新发起审核。"
        )
        logger.warning("auto_review_run_expired draft_id=%s review_id=%s", draft_id, row.id)

    def edit_draft(self, draft_id: str, patch: DraftEdit) -> DraftRow:
        row = self.get_draft(draft_id)
        if row.status in {
            ReviewStatus.READY_TO_PUBLISH.value,
            ReviewStatus.DRAFTBOX_CREATED.value,
            ReviewStatus.PUBLISHED.value,
            ReviewStatus.DISCARDED.value,
            ReviewStatus.DELETED.value,
        }:
            raise InvalidReviewTransition("已审核通过、已发布、已废弃或已删除的草稿不能直接修改")
        values = patch.model_dump(exclude_none=True)
        if "body" in values:
            values["body"] = normalize_plain_text(values["body"])
        field_map = {
            "title_options": "title_options_json",
            "tags": "tags_json",
            "card_script": "card_script_json",
        }
        for name, value in values.items():
            if name == "category":
                row.source_item.category = value.value
                row.source_item.category_confidence = 1.0
                row.source_item.metadata_json = {
                    **row.source_item.metadata_json,
                    "classification_reason": "人工修改",
                }
                continue
            setattr(row, field_map.get(name, name), value)
        row.version += 1
        row.status = ReviewStatus.PENDING_REVIEW.value
        self.session.flush()
        return row

    def review_draft(self, draft_id: str, command: ReviewCommand) -> DraftRow:
        previous = self.session.scalar(
            select(ReviewEventRow).where(
                ReviewEventRow.idempotency_key == command.idempotency_key
            )
        )
        if previous:
            if previous.draft_id != draft_id:
                raise InvalidReviewTransition("该幂等键已用于其他草稿")
            return self.get_draft(previous.draft_id)

        row = self.get_draft(draft_id)
        if row.status == ReviewStatus.READY_TO_PUBLISH.value:
            if command.action != "revoke":
                raise InvalidReviewTransition("该草稿已经审核通过；如需修改，请先撤销审核")
            row.status = ReviewStatus.NEEDS_REVISION.value
        elif row.status in {
            ReviewStatus.DRAFTBOX_CREATED.value,
            ReviewStatus.PUBLISHED.value,
            ReviewStatus.DISCARDED.value,
            ReviewStatus.DELETED.value,
        }:
            raise InvalidReviewTransition("该草稿已经审核通过、已发布、已废弃或已删除")
        else:
            if command.action == "revoke":
                raise InvalidReviewTransition("只有审核通过且尚未投递的草稿可以撤销审核")
            row.status = {
                "approve": ReviewStatus.READY_TO_PUBLISH.value,
                "reject": ReviewStatus.NEEDS_REVISION.value,
                "discard": ReviewStatus.DISCARDED.value,
            }[command.action]
        self.session.add(
            ReviewEventRow(
                draft_id=row.id,
                action=command.action,
                reviewer=command.reviewer,
                note=command.note,
                idempotency_key=command.idempotency_key,
            )
        )
        self.session.flush()
        return row

    def delete_draft(self, draft_id: str) -> DraftRow:
        """从默认审核列表移除草稿，同时保留来源、审核与投递审计。"""
        row = self.get_draft(draft_id)
        if row.status == ReviewStatus.DELETED.value:
            return row
        if row.status == ReviewStatus.PUBLISHED.value or self.session.scalar(
            select(PublicationRecordRow.id)
            .where(PublicationRecordRow.draft_id == draft_id)
            .limit(1)
        ):
            raise InvalidReviewTransition("已发布的草稿不能删除，以保留发布审计")
        if self.session.scalar(
            select(WechatPublicationJobRow.id)
            .where(WechatPublicationJobRow.draft_id == draft_id)
            .limit(1)
        ):
            raise InvalidReviewTransition("已有公众号投递记录的草稿不能删除，以保留投递审计")

        row.status = ReviewStatus.DELETED.value
        self.session.add(
            ReviewEventRow(
                draft_id=row.id,
                action="delete",
                reviewer="运营人员",
                note="已从默认审核列表删除，来源与审核记录保留",
                idempotency_key=f"delete:{row.id}",
            )
        )
        self.session.flush()
        return row
