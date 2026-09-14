"""自动审核后的受控草稿投递，终点严格限制为微信公众号草稿箱。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from fastapi.concurrency import run_in_threadpool

from app.config import Settings
from app.domain.models import ReviewCommand
from app.services.attachments import AttachmentError, PrivateAttachmentStore
from app.services.plain_text import extract_name_queries, normalize_wechat_description
from app.services.publication_preferences import load_publication_preferences
from app.services.source_snapshots import DraftSourceSnapshotStore
from app.services.runtime_settings import load_runtime_settings
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
) -> tuple[list[dict], dict]:
    """为这次改稿取一次联网补充资料；任何失败都只记日志，不阻断改稿。

    检索词优先用审核模型给出的 `search_queries`；没有时用正文里的外部名称做确定性兜底。
    返回（证据条目, 本次检索事实）：事实要一路带到**汇报文案**里，否则用户看不到
    “到底搜没搜、搜了什么”（真实反馈：感觉搜索没被使用）。
    """
    facts: dict = {"searched": False, "queries": [], "entries": 0}
    if not (settings.revision_search_enabled and settings.exa_mcp_enabled):
        return [], facts
    queries = [str(item) for item in (model_report.get("search_queries") or []) if str(item).strip()][:2]
    if not queries:
        queries = extract_name_queries(repository.get_draft(draft_id).body, limit=2)
    if not queries:
        return [], facts
    try:
        evidence = await ExaMcpSearchTool(settings).search(queries)
    except ExaMcpSearchError as exc:
        logger.warning("revision_search_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        return [], {**facts, "queries": queries, "failed": True}
    facts = {"searched": True, "queries": queries, "entries": len(evidence)}
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
            "联网检索「" + "、".join(queries) + f"」带回 {len(evidence)} 条资料，将在改稿时并入正文，"
            "并作为联网证据留在本文证据里。",
            "revising",
            draft_id=draft_id,
            search_queries=queries,
            search_entry_count=len(evidence),
        )
    return evidence, facts


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
    """审核开始前确定一次投递图片；之后只复用持久化选择，不再重新选择。

    `delivery_stale`（用户改过文案/配图或点了“重新选择配图”）必须重新选择——
    否则“重新选择配图”会变成空操作。
    """
    existing = repository.get_wechat_publication_for_draft(draft_id)
    if (
        existing is not None
        and existing.cover_asset_id
        and existing.state not in {"superseded", "delivery_stale"}
    ):
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
    """上传已确定的封面与正文插图；已上传过且未变化的部分**复用**，不重复占用永久素材配额。

    封面是永久素材（计入素材库配额），重复上传会让同一张图在素材库里堆成多份；
    正文图走 uploadimg 不占配额，但同样没必要重复上传。
    """
    job = await ensure_agent_selected_wechat_assets(settings, repository, draft_id)
    needs_cover = not job.cover_media_id
    needs_inline = bool(job.inline_asset_ids_json) and not job.inline_image_urls_json
    if not needs_cover and not needs_inline:
        logger.info(
            "wechat_assets_reused draft_id=%s cover_media_id_set=%s inline_ready=%s",
            draft_id, bool(job.cover_media_id), bool(job.inline_image_urls_json),
        )
        return job

    cover, inline = _selected_publication_illustrations(repository, draft_id, job)
    cover_asset = repository.get_publication_asset(cover.asset_id)
    inline_assets = [repository.get_publication_asset(item.asset_id) for item in inline]
    for asset in [cover_asset, *inline_assets]:
        repository.bind_publication_asset(draft_id, asset.id)
    try:
        store = PrivateAttachmentStore(settings)
        cover_content = store.read(cover_asset.object_key) if needs_cover else b""
        inline_contents = [store.read(asset.object_key) for asset in inline_assets] if needs_inline else []
    except AttachmentError as exc:
        raise RuntimeError("自动草稿无法读取私有图片素材") from exc
    try:
        async with WechatOfficialAccountTool(settings) as client:
            # 封面未变化时复用已有 media_id：永久素材会计入素材库配额，不能每投递一次就多一份。
            cover_media_id = job.cover_media_id or await client.upload_cover(cover_content, cover_asset.original_name)
            inline_urls = (
                [
                    await client.upload_inline_image(content, asset.original_name)
                    for asset, content in zip(inline_assets, inline_contents, strict=True)
                ]
                if needs_inline
                else [entry.get("url") for entry in (job.inline_image_urls_json or [])]
            )
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


async def ensure_cover_inline_url(settings: Settings, repository: ContentRepository, draft_id: str, job) -> str | None:
    """把封面图也上传为**正文图片**并缓存 URL（供“封面作为正文首图”使用）。

    正文只能引用 uploadimg 返回的微信地址；封面的永久素材 media_id 不能直接用在正文里。
    结果缓存在偏好表里按素材 id 复用，避免每次投递重复上传。
    """
    asset_id = getattr(job, "cover_asset_id", None)
    if not asset_id:
        return None
    cache_key = f"publication.cover_inline_url.{asset_id}"
    cached = repository.get_app_setting(cache_key)
    if cached:
        return cached
    asset = repository.get_publication_asset(asset_id)
    try:
        content = PrivateAttachmentStore(settings).read(asset.object_key)
        async with WechatOfficialAccountTool(settings) as client:
            url = await client.upload_inline_image(content, asset.original_name)
    except (AttachmentError, WechatOfficialAccountError, RuntimeError) as exc:
        logger.warning("cover_inline_upload_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        return None
    repository.set_app_setting(cache_key, url, updated_by="delivery")
    return url


async def ensure_footer_image_url(
    settings: Settings, repository: ContentRepository, preferences
) -> str | None:
    """准备固定结尾图的微信正文地址：优先用缓存，必要时上传一次。"""
    if not preferences.footer_image_configured or not preferences.footer_image_asset_id:
        return preferences.footer_image_url
    cached = repository.get_app_setting("publication.footer_inline_url")
    if cached:
        return cached
    asset = repository.get_publication_asset(preferences.footer_image_asset_id)
    try:
        content = PrivateAttachmentStore(settings).read(asset.object_key)
        async with WechatOfficialAccountTool(settings) as client:
            url = await client.upload_inline_image(content, asset.original_name)
    except (AttachmentError, WechatOfficialAccountError, RuntimeError) as exc:
        logger.warning("footer_image_upload_failed error_type=%s", type(exc).__name__)
        return preferences.footer_image_url
    repository.set_app_setting("publication.footer_inline_url", url, updated_by="delivery")
    repository.set_app_setting("publication.footer_image_url", url, updated_by="delivery")
    return url


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
    preferences = load_publication_preferences(repository)
    inline_payload = list(job.inline_image_urls_json or [])
    if preferences.cover_in_body and not any(
        isinstance(entry, dict) and entry.get("after_paragraph") == 0 for entry in inline_payload
    ):
        # 用户要求：封面图同时作为正文上方第一张图（位置 0 = 正文开头）。
        cover_inline_url = await ensure_cover_inline_url(settings, repository, draft_id, job)
        if cover_inline_url:
            inline_payload.insert(0, {"url": cover_inline_url, "after_paragraph": 0})
    footer_image_url = await ensure_footer_image_url(settings, repository, preferences)
    content_html = render_wechat_html(
        draft.body,
        inline_payload,
        footer_image_url=footer_image_url,
        include_text_footer=preferences.footer_text_enabled,
    )
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


async def apply_revision_from_review(
    settings: Settings,
    repository: ContentRepository,
    draft_id: str,
    review_id: str,
    chat_agent_run_id: str | None = None,
    extra_issues: list[str] | None = None,
) -> dict:
    """把审核意见真正应用到草稿上，覆盖为新版本。

    对话工具（`apply_revision_issues`）与审核流程共用同一条改稿实现，避免两套口径；
    `extra_issues` 是用户/Agent 临时补充的要求，与审核意见一起交给改稿模型。
    """
    rule_report: dict = {}
    model_report: dict = {}
    applied_review_id = review_id
    if review_id:
        run = repository.get_auto_review_run(review_id)
        if run.draft_id != draft_id:
            raise ValueError("该审核记录不属于这篇文章")
        rule_report = run.rule_report_json or {}
        model_report = run.model_report_json or {}
    extra = [str(item) for item in (extra_issues or []) if str(item).strip()]
    if extra:
        model_report = {**model_report, "issues": [*model_report.get("issues", []), *extra]}
    if not model_report and not rule_report:
        raise ValueError("没有可用的审核意见")
    issues = revision_issues(rule_report, model_report)
    search_evidence, search_facts = await _revision_search_evidence(
        settings, repository, draft_id, chat_agent_run_id, model_report
    )
    revised = await run_in_threadpool(
        AutoRevisionTool(settings, repository).invoke,
        draft_id,
        rule_report,
        model_report,
        draft=_model_draft_snapshot(repository, draft_id),
        search_evidence=search_evidence,
    )
    draft = repository.apply_auto_revision(
        draft_id,
        applied_review_id or "manual",
        revised.summary_cn,
        revised.body,
        revised.tags,
        {"issues": issues, "revision_count": 0, "source": "apply_revision_issues"},
        revised.article_shape,
    )
    logger.info(
        "revision_from_review_applied draft_id=%s review_id=%s version=%s issues=%s extra=%s searches=%s",
        draft_id, applied_review_id or "-", draft.version, len(issues), len(extra), search_facts.get("queries"),
    )
    return {
        "draft_id": draft.id,
        "draft_title": (draft.title_options_json or ["当前草稿"])[0],
        "version": draft.version,
        "issue_count": len(issues),
        "issues": issues,
        "article_shape": revised.article_shape,
        "searches": [search_facts] if search_facts.get("searched") else [],
    }


async def auto_review_and_create_wechat_draft(
    settings: Settings,
    repository: ContentRepository,
    draft_id: str,
    chat_agent_run_id: str | None = None,
    review_run_id: str | None = None,
    deliver: bool = True,
    revise: bool = True,
) -> dict:
    """审核通过后可创建远端草稿；不调用 submit_draft，因此绝不发表。

    `deliver=False` 表示“仅审核”：不选投递素材、不创建公众号草稿。
    `revise=False` 表示**只出意见、不改稿**（用户要“先看审核意见再说”时用这个）；
    默认 `revise=True`：只要审核给出可执行意见，通过与否都会先改稿一轮再复审。
    """
    settings = load_runtime_settings(settings)
    revision_count = 0
    # 联网补充的检索事实要一路带到汇报里：否则“搜索到底有没有被调用”在界面上无从判断。
    searches: list[dict] = []
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
            return {"review_id": selection_run.id, "status": "failed", "error": str(exc), "issues": [], "searches": []}
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
        can_revise = (
            revise
            and bool(issues)
            and review.error_message is None
            and revision_count < MAX_AUTO_REVIEW_REVISIONS
        )
        # 正文正在被重写（9–12 分钟）：本轮审核意见照常给出，但**不**并发改稿，
        # 否则两个写者抢同一个草稿版本号，后完成的那次会以唯一约束冲突失败。
        if can_revise:
            active_rewrite = repository.find_active_regeneration_run(
                draft_id,
                within_seconds=getattr(settings, "collection_job_timeout_seconds", 900),
                exclude_run_id=chat_agent_run_id or "",
            )
            if active_rewrite is not None:
                repository.finish_auto_review_run(
                    run.id, "revision_deferred", review.rule_report, review.model_report,
                    "正文正在重写，本轮不再并发改稿",
                )
                _record_review_event(
                    repository, chat_agent_run_id, "审核完成（改稿已让位给正在进行的重写）",
                    f"正文正在重写中，本轮只出意见（{len(issues)} 条）；重写完成后可再按这些意见改稿。",
                    "rejected",
                    review_id=run.id, revision_count=revision_count, issues=issues,
                )
                return {
                    "review_id": run.id,
                    "status": "reviewed_while_rewriting",
                    "passed": review.passed,
                    "revise_disabled": False,
                    "error": None,
                    "issues": issues,
                    "revision_count": revision_count,
                    "searches": searches,
                }
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
                repository, chat_agent_run_id,
                "审核完成（按你的要求未改稿）" if not revise else "自动审核未通过",
                detail, "rejected",
                review_id=run.id, revision_count=revision_count, issues=issues,
            )
            return {
                "review_id": run.id,
                # revise=False 时这是“只出意见”的结果：审核本身跑完了，只是没通过 / 没改稿。
                "status": "reviewed" if not revise else "failed",
                "passed": review.passed,
                "revise_disabled": not revise,
                "error": None if not revise else (review.error_message or "自动审核未通过"),
                "issues": issues,
                "revision_count": revision_count,
                "searches": searches,
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
            search_evidence, search_facts = await _revision_search_evidence(
                settings, repository, draft_id, chat_agent_run_id, review.model_report
            )
            if search_facts.get("searched"):
                searches.append(search_facts)
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
            return {"review_id": run.id, "status": "revision_failed", "error": str(exc), "issues": issues, "revision_count": revision_count, "searches": searches}
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
            "searches": searches,
        }
    if not settings.auto_wechat_draft_enabled:
        repository.finish_auto_review_run(run.id, "approved_no_delivery", review.rule_report, review.model_report, "自动创建公众号草稿开关未启用")
        return {
            "review_id": run.id,
            "status": "approved_no_delivery",
            "draft_id": draft.id,
            "revision_count": revision_count,
            "delivered": False,
            "searches": searches,
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
            "searches": searches,
        }
    except (WechatOfficialAccountError, PublicationAssetSelectionError, RuntimeError) as exc:
        repository.finish_auto_review_run(run.id, "delivery_failed", review.rule_report, review.model_report, str(exc))
        logger.warning("auto_wechat_draft_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        return {"review_id": run.id, "status": "delivery_failed", "error": str(exc), "revision_count": revision_count, "searches": searches}
