"""受控自动改稿 Tool：只按已保存的审核意见改写可编辑文案字段。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from langsmith.wrappers import wrap_openai
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings, api_key_for, base_url_for, model_for
from app.observability import langsmith_enabled
from app.services.model_errors import model_failure_message
from app.services.plain_text import (
    NATURAL_ARTICLE_MIN_CHARS,
    compose_natural_article,
    normalize_wechat_description,
)
from app.services.review_feedback import normalize_review_feedback
from app.services.term_policy import terminology_guidance
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.auto_revision")


class RevisionPayload(BaseModel):
    summary_cn: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1, max_length=10000)
    tags: list[str] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class AutoRevisionResult:
    summary_cn: str
    body: str
    tags: list[str]
    article_shape: dict[str, int]


class AutoRevisionError(RuntimeError):
    pass


def revision_issues(rule_report: dict, model_report: dict) -> list[str]:
    """汇总可展示、可执行的审核意见；不包含任何模型思维链。"""
    issues = normalize_review_feedback(rule_report.get("failures", []))
    for item in normalize_review_feedback(model_report.get("issues", [])):
        if item not in issues:
            issues.append(item)
    skipped = model_report.get("skipped")
    if skipped and not issues:
        issues.append(str(skipped))
    return issues[:12]


class AutoRevisionTool:
    def __init__(self, settings: Settings, repository: ContentRepository):
        self.settings = settings
        self.repository = repository

    def invoke(self, draft_id: str, rule_report: dict, model_report: dict, draft=None) -> AutoRevisionResult:
        selected_model = model_for(self.settings, "content")
        selected_api_key = api_key_for(self.settings, "content")
        if not (self.settings.llm_enabled and selected_api_key and selected_model):
            raise AutoRevisionError("自动改稿模型未启用或配置不完整")
        draft = draft or self.repository.get_draft(draft_id)
        issues = revision_issues(rule_report, model_report)
        if not issues:
            raise AutoRevisionError("审核未提供可执行的改稿意见")
        evidence = draft.evidence_json[:12]
        prompt = (
            "你是科技资讯编辑，只能根据来源证据和审核意见改写草稿。返回 JSON：summary_cn、body、tags。"
            "不可改变来源名称、来源链接、原文标题或任何未被证据支持的事实；不要编造数据、人物或结论。"
            "body 必须使用 4 到 8 个自然段，以空行分隔；应自然覆盖背景、技术或过程、价值与边界、后续观察，"
            "但不要在正文写出这些名称、显式段落标题、编号、原文标题或链接。"
            "body 必须为纯文本，不用 Markdown、HTML。语气自然、生活化，但避免绝对化表述。每段段首缩进和原文标题尾注由服务端处理。"
            + terminology_guidance()
            + "不得把术语替换成无来源的近义说法。"
            "summary_cn 是 30 到 60 字、吸引点击但不夸张的一句话导语。\n"
            f"来源名称：{draft.source_name}\n原文标题：{draft.source_item.title}\n来源链接：{draft.source_url}\n"
            f"来源证据：{json.dumps(evidence, ensure_ascii=False)}\n"
            f"当前摘要：{draft.summary_cn}\n当前正文：{draft.body[:7000]}\n"
            f"审核意见：{json.dumps(issues, ensure_ascii=False)}"
        )
        try:
            logger.info(
                "auto_revision_started draft_id=%s model_category=content model=%s",
                draft_id,
                selected_model,
            )
            client = OpenAI(
                api_key=selected_api_key,
                base_url=base_url_for(self.settings, "content") or None,
                max_retries=self.settings.content_llm_max_retries,
                timeout=self.settings.content_llm_timeout_seconds,
            )
            client = wrap_openai(client) if langsmith_enabled(self.settings) else client
            response = client.chat.completions.create(
                model=selected_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.15,
            )
            payload = RevisionPayload.model_validate(json.loads(response.choices[0].message.content or "{}"))
            body, article_shape = compose_natural_article(
                payload.body,
                min_chars=max(self.settings.draft_body_min_chars, NATURAL_ARTICLE_MIN_CHARS),
            )
            logger.info(
                "auto_revision_article_validated draft_id=%s paragraph_count=%s",
                draft_id,
                article_shape["paragraph_count"],
            )
            return AutoRevisionResult(
                summary_cn=normalize_wechat_description(payload.summary_cn),
                body=body,
                tags=[tag.strip("# ") for tag in payload.tags if tag.strip("# ")],
                article_shape=article_shape,
            )
        except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as exc:
            logger.warning("auto_revision_invalid draft_id=%s error_type=%s", draft_id, type(exc).__name__)
            raise AutoRevisionError("自动改稿输出不符合结构") from exc
        except Exception as exc:
            logger.warning("auto_revision_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
            raise AutoRevisionError(
                model_failure_message(exc, "自动改稿", self.settings.content_llm_timeout_seconds)
            ) from exc
