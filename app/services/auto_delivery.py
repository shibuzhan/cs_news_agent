"""自动审核后的受控草稿投递，终点严格限制为微信公众号草稿箱。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from fastapi.concurrency import run_in_threadpool

from app.config import Settings
from app.domain.models import ReviewCommand
from app.services.attachments import AttachmentError, PrivateAttachmentStore
from app.services.plain_text import extract_name_queries, normalize_wechat_description
from app.services.source_snapshots import DraftSourceSnapshotStore
from app.services.wechat_official import WechatOfficialAccountError, render_wechat_html
from app.storage.repositories import ContentRepository
from app.tools.auto_review import AutoReviewTool
from app.tools.auto_revision import AutoRevisionError, AutoRevisionTool, revision_issues
from app.tools.illustration_planner import IllustrationPlanner, PublicationAssetSelectionError
from app.tools.search_tools import ExaMcpSearchError, ExaMcpSearchTool
from app.tools.wechat_official_account import WechatOfficialAccountTool


logger = logging.getLogger("news_agent.auto_delivery")
MAX_AUTO_REVIEW_REVISIONS = 1


async def _revision_search_evidence(
    settings: Settings,
    repository: ContentRepository,
    draft_id: str,
    chat_agent_run_id: str | None,
    model_report: dict,
) -> list[dict]:
    """为这次改稿取一次联网补充资料；任何失败都只记日志，不阻断改稿。

    检索词优先用审核模型给出的 `search_queries`；没有时用正文里的外部名称做确定性兜底。
    """
    if not (settings.revision_search_enabled and settings.exa_mcp_enabled):
        return []
    queries = [str(item) for item in (model_report.get("search_queries") or []) if str(item).strip()][:2]
    if not queries:
        queries = extract_name_queries(repository.get_draft(draft_id).body, limit=2)
    if not queries:
        return []
    try:
        evidence = await ExaMcpSearchTool(settings).search(queries)
    except ExaMcpSearchError as exc:
        logger.warning("revision_search_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        return []
    if evidence:
        # 让审核也看得到这次补充：否则联网写入的事实会被判成“来源证据中未出现”。
        try:
            appended = repository.append_draft_evidence(draft_id, evidence)
            repository.session.commit()
            logger.info("revision_search_evidence_persisted draft_id=%s entries=%s", draft_id, appended)
        except Exception as exc:
            repository.session.rollback()
            logger.warning(
                "revision_search_evidence_persist_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__
            )
    if evidence and chat_agent_run_id:
        _record_review_event(
            repository,
            chat_agent_run_id,
            "改稿前已联网补充",
            f"已按审核意见为 {len(evidence)} 个外部名称取回补充资料，将在改稿时并入正文。",
            "revising",
            draft_id=draft_id,
            search_query_count=len(queries),
        )
    return evidence


def _model_draft_snapshot(repository: ContentRepository, draft_id: str) -> SimpleNamespace:
    """在事件循环线程加载审核所需字段，模型线程不持有 SQLAlchemy Session。"""
    draft = repository.get_draft(draft_id)
    source = draft.source_item
    return SimpleNamespace(
        body=draft.body,
        source_url=str(draft.source_url or ""),
        source_name=draft.source_name,
        title_options_json=list(draft.title_options_json or []),
        tags_json=list(draft.tags_json or []),
        content_plan_json=dict(draft.content_plan_json or {}),
        evidence_json=list(draft.evidence_json or []),
        summary_cn=draft.summary_cn,
        source_item=SimpleNamespace(title=source.title, source_kind=source.source_kind),
    )


def _record_review_event(
    repository: ContentRepository,
    chat_agent_run_id: str | None,
    title: str,
    detail: str,
    state: str,
    **metadata: object,
) -> None:
    if chat_agent_run_id:
        repository.add_chat_agent_event(
            chat_agent_run_id,
            title,
            detail,
            "failed" if state == "rejected" else "running" if state in {"running", "revising"} else "completed",
            metadata={"phase": "review", "state": state, **metadata},
        )


async def ensure_agent_selected_wechat_assets(
    settings: Settings, repository: ContentRepository, draft_id: str,
):
    """审核开始前确定一次投递图片；之后只复用持久化选择，不再重新选择。"""
    existing = repository.get_wechat_publication_for_draft(draft_id)
    if existing is not None and existing.cover_asset_id and existing.state != "superseded":
        return existing
    draft = repository.get_draft(draft_id)
    illustrations = repository.list_draft_illustrations(draft_id)
    def load_source_image(asset_id: str) -> bytes | None:
        """供视觉选图读取真实截图字节；AI 配图不读。"""
        asset = repository.get_publication_asset(asset_id)
        if not str(asset.original_name or "").startswith("source-"):
            return None
        return PrivateAttachmentStore(settings).read(asset.object_key)

    selection = await run_in_threadpool(
        IllustrationPlanner(settings).decide_publication_assets, draft, illustrations, load_source_image
    )
    return repository.save_wechat_asset_selection(
        draft_id, selection.cover_asset_id, selection.inline_asset_ids
    )


def _selected_publication_illustrations(repository: ContentRepository, draft_id: str, job):
    """从审核前固化的选择恢复图片；已删除或缺失时安全停止，不回退到历史素材。"""
    illustrations = {item.asset_id: item for item in repository.list_draft_illustrations(draft_id)}
    if not job.cover_asset_id or job.cover_asset_id not in illustrations:
        raise PublicationAssetSelectionError("已选择的封面图已删除或不可用，请重新生成或重新选择配图后再审核")
    missing_inline = [asset_id for asset_id in (job.inline_asset_ids_json or []) if asset_id not in illustrations]
    if missing_inline:
        raise PublicationAssetSelectionError("已选择的正文插图已删除或不可用，请重新生成或重新选择配图后再审核")
    cover = illustrations[job.cover_asset_id]
    inline = [illustrations[asset_id] for asset_id in (job.inline_asset_ids_json or [])]
    return cover, inline


async def prepare_agent_selected_wechat_assets(
    settings: Settings, repository: ContentRepository, draft_id: str,
):
    """上传审核前已确定的封面和插图；绝不在投递或重投时再次选择图片。"""
    job = await ensure_agent_selected_wechat_assets(settings, repository, draft_id)
    if job.cover_media_id:
        return job

    cover, inline = _selected_publication_illustrations(repository, draft_id, job)
    cover_asset = repository.get_publication_asset(cover.asset_id)
    inline_assets = [repository.get_publication_asset(item.asset_id) for item in inline]
    for asset in [cover_asset, *inline_assets]:
        repository.bind_publication_asset(draft_id, asset.id)
    try:
        store = PrivateAttachmentStore(settings)
        cover_content = store.read(cover_asset.object_key)
        inline_contents = [store.read(asset.object_key) for asset in inline_assets]
    except AttachmentError as exc:
        raise RuntimeError("自动草稿无法读取私有图片素材") from exc
    try:
        async with WechatOfficialAccountTool(settings) as client:
            cover_media_id = await client.upload_cover(cover_content, cover_asset.original_name)
            inline_urls = [
                await client.upload_inline_image(content, asset.original_name)
                for asset, content in zip(inline_assets, inline_contents, strict=True)
            ]
    except (WechatOfficialAccountError, RuntimeError) as exc:
        repository.mark_wechat_status(job.id, "draft_failed", error_message=str(exc))
        logger.warning("wechat_asset_prepare_failed draft_id=%s job_id=%s error_type=%s", draft_id, job.id, type(exc).__name__)
        raise
    positioned_urls = [
        {"url": url, "after_paragraph": item.placement_after_paragraph}
        for item, url in zip(inline, inline_urls, strict=True)
    ]
    return repository.save_wechat_publication(
        draft_id, cover_asset.id, cover_media_id, [asset.id for asset in inline_assets], positioned_urls
    )


def release_source_snapshot_after_delivery(settings: Settings, repository: ContentRepository, draft_id: str) -> bool:
    """投递进公众号草稿箱后释放来源快照。

    快照的生命周期是“首次读取 → 保存 → **直到该项目投递进草稿箱**”。此前在审核通过时就删除，
    导致通过之后的重写/重审/换图无法再复用来源正文。清理失败只记日志，不影响投递结果。
    """
    try:
        deleted = DraftSourceSnapshotStore(settings, repository).delete_after_approval(draft_id)
        logger.info("source_snapshot_released_after_delivery draft_id=%s deleted=%s", draft_id, deleted)
        return deleted
    except Exception as exc:
        logger.warning("source_snapshot_release_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        return False


async def retry_agent_selected_wechat_draft(
    settings: Settings, repository: ContentRepository, draft_id: str,
):
    """重试/更新草稿箱投递：复用已审核文案，必要时按当前图片重新上传。

    - 远端草稿已存在且标记为“投递已过期”：用 `draft/update` **原地覆盖**远端草稿内容；
    - 尚未投递：走 `draft/add` 新建；
    - 覆盖失败（例如远端草稿已被删除）时回退为新建，不让投递彻底失败。
    """
    draft = repository.get_draft(draft_id)
    if draft.status not in {"ready_to_publish", "draftbox_created"}:
        raise RuntimeError("只有审核通过的文章可以投递或更新公众号草稿")
    job = repository.get_wechat_publication_for_draft(draft_id)
    if job is None or not job.cover_media_id:
        job = await prepare_agent_selected_wechat_assets(settings, repository, draft_id)
    if not job.cover_media_id:
        raise RuntimeError("投递记录缺少公众号封面素材，无法投递")
    title = (draft.title_options_json or ["未命名草稿"])[0]
    digest = normalize_wechat_description(draft.summary_cn)
    content_html = render_wechat_html(draft.body, job.inline_image_urls_json or [])
    if job.wechat_draft_media_id:
        try:
            async with WechatOfficialAccountTool(settings) as client:
                await client.update_draft(
                    media_id=job.wechat_draft_media_id,
                    title=title,
                    digest=digest,
                    content_html=content_html,
                    source_url=str(draft.source_url or ""),
                    cover_media_id=job.cover_media_id,
                )
        except WechatOfficialAccountError as exc:
            # 远端草稿可能已被删除：退化为新建，而不是让用户卡在过期状态。
            logger.warning("wechat_draft_update_failed draft_id=%s job_id=%s error_type=%s", draft_id, job.id, type(exc).__name__)
            repository.mark_wechat_status(job.id, "draft_failed", error_message=str(exc))
            try:
                async with WechatOfficialAccountTool(settings) as client:
                    media_id = await client.create_draft(
                        title=title, digest=digest, content_html=content_html,
                        source_url=str(draft.source_url or ""), cover_media_id=job.cover_media_id,
                    )
            except WechatOfficialAccountError as create_exc:
                repository.mark_wechat_status(job.id, "draft_failed", error_message=str(create_exc))
                raise
            logger.info("wechat_draft_recreated draft_id=%s job_id=%s", draft_id, job.id)
            created = repository.mark_wechat_draft_created(job.id, media_id)
            release_source_snapshot_after_delivery(settings, repository, draft_id)
            return created
        logger.info("wechat_draft_updated draft_id=%s job_id=%s media_id=%s", draft_id, job.id, job.wechat_draft_media_id)
        updated = repository.mark_wechat_draft_updated(job.id)
        release_source_snapshot_after_delivery(settings, repository, draft_id)
        return updated
    try:
        async with WechatOfficialAccountTool(settings) as client:
            media_id = await client.create_draft(
                title=title, digest=digest, content_html=content_html,
                source_url=str(draft.source_url or ""), cover_media_id=job.cover_media_id,
            )
    except WechatOfficialAccountError as exc:
        repository.mark_wechat_status(job.id, "draft_failed", error_message=str(exc))
        logger.warning("wechat_draft_retry_failed draft_id=%s job_id=%s", draft_id, job.id)
        raise
    logger.info("wechat_draft_retry_succeeded draft_id=%s job_id=%s", draft_id, job.id)
    created = repository.mark_wechat_draft_created(job.id, media_id)
    release_source_snapshot_after_delivery(settings, repository, draft_id)
    return created


async def auto_review_and_create_wechat_draft(
    settings: Settings,
    repository: ContentRepository,
    draft_id: str,
    chat_agent_run_id: str | None = None,
    review_run_id: str | None = None,
    deliver: bool = True,
) -> dict:
    """审核通过后可创建远端草稿；不调用 submit_draft，因此绝不发表。

    `deliver=False` 表示“仅审核”：不选投递素材、不创建公众号草稿，只给出审核意见与
    按意见进行的一轮改稿。只要审核给出可执行意见，**通过与否都会先改稿一轮再复审**。
    """
    revision_count = 0
    if review_run_id:
        selection_run = repository.mark_auto_review_run_running(review_run_id)
    else:
        selection_run = repository.create_auto_review_run(draft_id, chat_agent_run_id)
    selection_job = None
    if deliver:
        try:
            selection_job = await ensure_agent_selected_wechat_assets(settings, repository, draft_id)
        except PublicationAssetSelectionError as exc:
            repository.finish_auto_review_run(selection_run.id, "failed", {}, {}, str(exc))
            _record_review_event(
                repository, chat_agent_run_id, "投递配图未确定", str(exc), "rejected",
                review_id=selection_run.id,
            )
            return {"review_id": selection_run.id, "status": "failed", "error": str(exc), "issues": []}
        _record_review_event(
            repository,
            chat_agent_run_id,
            "投递配图已确定",
            f"已在审核前确定 1 张封面图和 {len(selection_job.inline_asset_ids_json or [])} 张正文插图；后续审核与重投将复用该选择。",
            "completed",
            review_id=selection_run.id,
            cover_asset_id=selection_job.cover_asset_id,
            inline_asset_ids=list(selection_job.inline_asset_ids_json or []),
        )
    pending_run_id = selection_run.id
    while True:
        if pending_run_id:
            run = repository.mark_auto_review_run_running(pending_run_id)
            pending_run_id = None
        else:
            run = repository.create_auto_review_run(draft_id, chat_agent_run_id)
        _record_review_event(
            repository, chat_agent_run_id, "正在审核文案",
            "先跑规则检查（字数、段落、来源尾注），再调用审核模型；模型审核通常需要几分钟，请稍候。",
            "running",
            review_id=run.id, revision_count=revision_count,
        )
        review = await run_in_threadpool(
            AutoReviewTool(settings, repository).invoke,
            draft_id,
            draft=_model_draft_snapshot(repository, draft_id),
        )
        # 模型审核通常要几分钟且没有流式输出：把“规则通过/模型返回”写成事件，界面才有进度可看。
        _record_review_event(
            repository, chat_agent_run_id, "审核模型已返回",
            "评分 {score}/{full}；{count} 条意见。".format(
                score=int(review.model_report.get("score") or 0),
                full=getattr(settings, "auto_review_pass_score", 85),
                count=len(review.model_report.get("issues") or []),
            ),
            "running",
            review_id=run.id, revision_count=revision_count,
        )
        issues = revision_issues(review.rule_report, review.model_report)
        can_revise = bool(issues) and review.error_message is None and revision_count < MAX_AUTO_REVIEW_REVISIONS
        if review.passed and not can_revise:
            _record_review_event(
                repository, chat_agent_run_id, "自动审核通过",
                "文字与插图审核已通过。" + ("进入受控草稿投递步骤。" if deliver else "本次仅审核，未创建公众号草稿。"),
                "approved",
                review_id=run.id, revision_count=revision_count,
            )
            break

        repository.finish_auto_review_run(
            run.id,
            "revision_required" if can_revise else "failed",
            review.rule_report,
            review.model_report,
            review.error_message,
        )
        if not can_revise:
            detail = review.error_message or "；".join(issues) or "自动审核未通过"
            _record_review_event(
                repository, chat_agent_run_id, "自动审核未通过", detail, "rejected",
                review_id=run.id, revision_count=revision_count, issues=issues,
            )
            return {
                "review_id": run.id,
                "status": "failed",
                "error": review.error_message or "自动审核未通过",
                "issues": issues,
                "revision_count": revision_count,
            }

        revision_count += 1
        _record_review_event(
            repository, chat_agent_run_id, f"自动改稿中（{revision_count}/{MAX_AUTO_REVIEW_REVISIONS}）",
            "审核已通过，仍按意见做一轮优化后复审。" if review.passed
            else "正根据上一轮审核意见改写文案，并保持来源事实字段不变。",
            "revising",
            review_id=run.id, revision_count=revision_count, issues=issues,
        )
        try:
            # 联网补充搭在“本来就会发生”的这次改稿上：审核模型给出待说明的外部名称，
            # 这里只做检索（非模型调用），改稿时顺带把说明补进正文。
            search_evidence = await _revision_search_evidence(
                settings, repository, draft_id, chat_agent_run_id, review.model_report
            )
            revised = await run_in_threadpool(
                AutoRevisionTool(settings, repository).invoke,
                draft_id,
                review.rule_report,
                review.model_report,
                draft=_model_draft_snapshot(repository, draft_id),
                search_evidence=search_evidence,
            )
            draft = repository.apply_auto_revision(
                draft_id,
                run.id,
                revised.summary_cn,
                revised.body,
                revised.tags,
                {"issues": issues, "revision_count": revision_count},
                revised.article_shape,
            )
        except AutoRevisionError as exc:
            repository.finish_auto_review_run(
                run.id, "revision_failed", review.rule_report, review.model_report, str(exc)
            )
            _record_review_event(
                repository, chat_agent_run_id, "自动改稿失败", str(exc), "rejected",
                review_id=run.id, revision_count=revision_count,
            )
            return {"review_id": run.id, "status": "revision_failed", "error": str(exc), "issues": issues, "revision_count": revision_count}
        _record_review_event(
            repository, chat_agent_run_id, f"自动改稿完成（{revision_count}/{MAX_AUTO_REVIEW_REVISIONS}）",
            f"已生成文案版本 {draft.version}，正在重新审核。", "running",
            review_id=run.id, revision_count=revision_count, draft_version=draft.version,
        )

    draft = repository.review_draft(
        draft_id,
        ReviewCommand(
            reviewer="AI 审核", action="approve", note="自动审核通过，允许创建公众号草稿", idempotency_key=f"auto-review:{run.id}",
        ),
    )
    # 审核通过**不删**来源快照：README 快照要保留到真正投递进公众号草稿箱，
    # 这样审核通过后的重写/重审/换图仍能复用第一次读到的来源正文。
    if not deliver:
        repository.finish_auto_review_run(
            run.id, "approved_no_delivery", review.rule_report, review.model_report, "本次仅运行审核，未创建公众号草稿"
        )
        return {
            "review_id": run.id,
            "status": "approved_no_delivery",
            "draft_id": draft.id,
            "revision_count": revision_count,
            "delivered": False,
        }
    if not settings.auto_wechat_draft_enabled:
        repository.finish_auto_review_run(run.id, "approved_no_delivery", review.rule_report, review.model_report, "自动创建公众号草稿开关未启用")
        return {
            "review_id": run.id,
            "status": "approved_no_delivery",
            "draft_id": draft.id,
            "revision_count": revision_count,
            "delivered": False,
        }
    try:
        job = await retry_agent_selected_wechat_draft(settings, repository, draft_id)
        repository.finish_auto_review_run(run.id, "wechat_draft_created", review.rule_report, review.model_report, wechat_job_id=job.id)
        logger.info("auto_wechat_draft_created draft_id=%s job_id=%s", draft_id, job.id)
        return {
            "review_id": run.id,
            "status": "wechat_draft_created",
            "draft_id": draft.id,
            "wechat_job_id": job.id,
            "revision_count": revision_count,
            "delivered": True,
        }
    except (WechatOfficialAccountError, PublicationAssetSelectionError, RuntimeError) as exc:
        repository.finish_auto_review_run(run.id, "delivery_failed", review.rule_report, review.model_report, str(exc))
        logger.warning("auto_wechat_draft_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        return {"review_id": run.id, "status": "delivery_failed", "error": str(exc), "revision_count": revision_count}
