"""受控自动改稿 Tool：只按已保存的审核意见改写可编辑文案字段。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from langsmith.wrappers import wrap_openai
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import Settings, api_key_for, base_url_for, model_for
from app.observability import langsmith_enabled
from app.services.model_errors import model_failure_message
from app.services.plain_text import (
    MIN_HARD_BODY_CHARS,
    NaturalArticleError,
    article_length_band,
    compose_natural_article,
    normalize_wechat_description,
)
from app.services.review_feedback import normalize_review_feedback
from app.services.term_policy import terminology_guidance
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.auto_revision")


def _preference_rules(repository) -> str:
    """运营者的长期偏好（app_settings）：生成、改稿、审核共用同一套，避免互相打架。"""
    try:
        from app.services.publication_preferences import preference_rules_block

        return preference_rules_block(repository)
    except Exception:
        return ""


class RevisionPayload(BaseModel):
    summary_cn: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1, max_length=10000)
    tags: list[str] = Field(default_factory=list, max_length=10)

    # 与文案子 Agent 相同的形态兼容：模型偶尔把列表字段写成字符串、把正文写成段落数组。
    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,，、;；]+", value) if item.strip()][:10]
        return value

    @field_validator("body", mode="before")
    @classmethod
    def _coerce_body(cls, value: object) -> object:
        if isinstance(value, list):
            return "\n\n".join(str(item).strip() for item in value if str(item).strip())
        return value


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


def _search_evidence_section(search_evidence: list[dict] | None) -> str:
    """改稿提示词里的联网补充段落；没有资料时返回空串，保持原提示词不变。"""
    if not search_evidence:
        return ""
    lines: list[str] = []
    for entry in search_evidence[:2]:
        if not isinstance(entry, dict):
            continue
        title = " ".join(str(entry.get("title") or "").split())[:120]
        content = " ".join(str(entry.get("content") or "").split())[:1200]
        if not content:
            continue
        lines.append(f"- {title}：{content}" if title else f"- {content}")
    if not lines:
        return ""
    return (
        "联网补充资料（只能用于为正文里的外部名称与背景补一句准确说明，不得用于编造项目事实、"
        "不得据此改写来源事实，也不要把正文写成安装教程）：\n"
        + "\n".join(lines)
        + "\n"
    )


class AutoRevisionTool:
    def __init__(self, settings: Settings, repository: ContentRepository):
        from app.services.runtime_settings import load_runtime_settings
        self.settings = load_runtime_settings(settings)
        self.repository = repository

    def _compose_with_length_repair(
        self, *, client, model: str, prompt: str, minimum_chars: int, draft_id: str
    ) -> tuple[RevisionPayload, str, dict]:
        """生成改稿正文；只因为“删重复删过头”而不合格时，带反馈重试一次。

        真实故障：审核意见是“第 2/3/4 段重复”，模型把重复删掉后正文掉到 1400 字以下，
        `compose_natural_article` 直接抛错 → 整轮改稿失败（运行报“按意见改稿这一步失败了”），
        用户拿到的还是原稿。这类失败是可修复的：告诉它当前字数与下限再写一次。
        """
        last_error: NaturalArticleError | None = None
        current_prompt = prompt
        for attempt in (1, 2):
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": current_prompt}],
                response_format={"type": "json_object"},
                temperature=0.15,
            )
            payload = RevisionPayload.model_validate(json.loads(response.choices[0].message.content or "{}"))
            try:
                body, article_shape = compose_natural_article(payload.body, min_chars=minimum_chars)
            except NaturalArticleError as exc:
                last_error = exc
                logger.warning(
                    "auto_revision_length_retry draft_id=%s attempt=%s reason=%s", draft_id, attempt, exc
                )
                current_prompt = (
                    f"{prompt}\n\n上一次改稿被拒绝：{exc}。"
                    f"请重写正文：**必须不少于 {minimum_chars} 个中文字符**，"
                    "删掉重复表述后要用来源证据里的其他事实补足篇幅，不要靠重复原话凑字数。"
                )
                continue
            return payload, body, article_shape
        assert last_error is not None
        raise last_error

    def invoke(
        self,
        draft_id: str,
        rule_report: dict,
        model_report: dict,
        draft=None,
        search_evidence: list[dict] | None = None,
    ) -> AutoRevisionResult:
        selected_model = model_for(self.settings, "content")
        selected_api_key = api_key_for(self.settings, "content")
        if not (self.settings.llm_enabled and selected_api_key and selected_model):
            raise AutoRevisionError("自动改稿模型未启用或配置不完整")
        draft = draft or self.repository.get_draft(draft_id)
        issues = revision_issues(rule_report, model_report)
        if not issues:
            raise AutoRevisionError("审核未提供可执行的改稿意见")
        evidence = draft.evidence_json[:12]
        target_low, target_high, minimum_chars, maximum_chars = article_length_band(
            self.settings.draft_body_min_chars, self.settings.draft_body_max_chars
        )
        search_section = _search_evidence_section(search_evidence)
        prompt = (
            "你是一个每天都在翻技术资讯的分享者，正在按审核意见改写自己的稿子；"
            "只能根据来源证据和审核意见改写，不得改变来源名称、来源链接、原文标题或任何未被证据支持的事实，"
            "也不要编造数据、人物或结论。返回 JSON：summary_cn、body、tags。"
            "body 必须使用 4 到 8 个自然段，以空行分隔；"
            # 审核意见以语气与通顺为主，改稿就按这个优先级处理，不要顺手重写全文。
            "**改写优先级**：先解决审核意见里的语气与通顺问题（这两类占绝大多数），"
            "其余部分尽量保留原文表述，不要为了改细节而重写整篇。"
            f"正文长度目标是 {target_low} 到 {target_high} 个中文字符——"
            f"硬性要求：不得少于 {minimum_chars} 个中文字符，也不得超过 {maximum_chars} 个中文字符"
            "（超过上限会被规则审核直接判为不合格，不要写到接近上限）。"
            "删减重复或空泛内容时也不得低于下限，宁可保留必要的细节和判断依据。"
            "应自然覆盖背景、技术或过程、价值与边界、后续观察，"
            "但不要在正文写出这些名称、显式段落标题、编号、原文标题或链接。"
            "body 必须为纯文本，不用 Markdown、HTML。每段段首缩进和原文标题尾注由服务端处理。"
            "语气自然、直接、生活化，避免绝对化表述；"
            "数字保持中文可读写法（41987 写成约 4.2 万），量级与来源一致，不要为了“精确”改回原始位数；"
            "保持分享者口吻：第一人称“我看到的”、对读者说“你会用到”都可以用，**不要把文章改成百科定义句"
            "（“X 是……的一个……”）、研报腔或通篇无主语的客观陈述**；"
            "段末如果只是复述本段，删掉，不要补“总的来说”“这意味着”这类概括句。"
            "不得使用“如果把它放在……的语境里看”“从某种角度看”“在一定程度上”这类翻译腔或空泛铺垫，"
            "能直接说清楚的就直接说；来源没有支持的细节就删掉或简化，不要用含糊措辞掩盖。"
            + terminology_guidance()
            + _preference_rules(self.repository)
            + "不得把术语替换成无来源的近义说法。"
            "summary_cn 是 30 到 60 字、吸引点击但不夸张的一句话导语。\n"
            + search_section
            + f"来源名称：{draft.source_name}\n原文标题：{draft.source_item.title}\n来源链接：{draft.source_url}\n"
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
            hard_minimum = max(
                article_length_band(
                    self.settings.draft_body_min_chars, self.settings.draft_body_max_chars
                )[2],
                MIN_HARD_BODY_CHARS,
            )
            payload, body, article_shape = self._compose_with_length_repair(
                client=client,
                model=selected_model,
                prompt=prompt,
                minimum_chars=hard_minimum,
                draft_id=draft_id,
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
        except NaturalArticleError as exc:
            # 正文形态由服务端规则决定；这里给出具体原因，避免只显示“不符合结构”。
            logger.warning("auto_revision_article_rejected draft_id=%s reason=%s", draft_id, exc)
            raise AutoRevisionError(f"自动改稿正文不符合要求：{exc}") from exc
        except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as exc:
            logger.warning("auto_revision_invalid draft_id=%s error_type=%s", draft_id, type(exc).__name__)
            raise AutoRevisionError("自动改稿输出不符合结构") from exc
        except Exception as exc:
            logger.warning("auto_revision_failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
            raise AutoRevisionError(
                model_failure_message(exc, "自动改稿", self.settings.content_llm_timeout_seconds)
            ) from exc
