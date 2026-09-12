"""自动审核 Tool：规则先行，模型仅输出结构化结论，不返回思维过程。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.agents.content_task_agents import RestrictedContentTaskAgent
from app.config import Settings, api_key_for, model_for
from app.services.model_errors import model_failure_message
from app.services.plain_text import NATURAL_ARTICLE_MIN_CHARS, normalize_plain_text
from app.services.review_feedback import normalize_review_feedback, review_feedback_text
from app.services.term_policy import terminology_guidance
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.auto_review")


@dataclass(frozen=True)
class AutoReviewResult:
    passed: bool
    rule_report: dict
    model_report: dict
    error_message: str | None = None


def _review_score(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("审核评分必须是 0 到 100 的整数")
    try:
        score = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("审核模型未返回有效评分") from exc
    if not 0 <= score <= 100:
        raise ValueError("审核评分必须在 0 到 100 之间")
    return score


def _blocking_issue_count(values: object) -> int:
    if not isinstance(values, list):
        return 0
    return sum(
        1
        for item in values
        if isinstance(item, dict)
        and str(item.get("severity", "")).strip().lower() in {"critical", "blocking", "fatal"}
    )


def rule_review(draft, min_body_chars: int = 800, max_body_chars: int = 3200) -> dict:
    # 这里不能先调用 normalize_plain_text：它会按设计去除段首空白，
    # 而段首两个全角空格正是本项目需要核验的格式规则。
    min_body_chars = max(min_body_chars, NATURAL_ARTICLE_MIN_CHARS)
    body = draft.body.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [item for item in body.split("\n\n") if item.strip()]
    failures: list[str] = []
    body_without_footer = "\n\n".join(
        item for item in paragraphs if not item.lstrip("　 ").startswith("原文标题：")
    )
    if not draft.source_url.startswith(("https://", "http://")):
        failures.append("缺少可追溯原文链接")
    if not draft.source_name or not (draft.title_options_json or []):
        failures.append("缺少来源或标题")
    if not draft.tags_json:
        failures.append("缺少内容标签")
    if body_without_footer and not (min_body_chars <= len(body_without_footer) <= max_body_chars):
        failures.append(f"正文长度不在 {min_body_chars} 到 {max_body_chars} 字符范围内")
    source_kind = str(getattr(getattr(draft, "source_item", None), "source_kind", "")).casefold()
    if source_kind != "github" and body.count("原文标题：") != 1:
        failures.append("原文标题尾注必须且只能出现一次")
    if re.search(r"https?://", body):
        failures.append("正文不应重复放置原文链接")
    if any(marker in body for marker in ("先说结论", "背景介绍：", "实现过程：", "未来展望：")):
        failures.append("正文不应使用模板化显式段落标题")
    content_paragraphs = [item for item in paragraphs if not item.lstrip("　 ").startswith("原文标题：")]
    if not 4 <= len(content_paragraphs) <= 8:
        failures.append("正文应为 4 到 8 个自然段")
    if any(not item.startswith("　　") for item in content_paragraphs):
        failures.append("正文自然段需要两个全角空格缩进")
    content_plan = getattr(draft, "content_plan_json", {}) or {}
    logical_sections = content_plan.get("logical_sections", [])
    article_shape = content_plan.get("article_shape", {})
    # 旧草稿可能仍带有逻辑部分审计，但不再将其作为审核门槛。
    return {
        "passed": not failures,
        "failures": failures,
        "body_chars": len(body_without_footer),
        "paragraph_count": len(content_paragraphs),
        "logical_sections": logical_sections,
        "article_shape": article_shape,
    }


class AutoReviewTool:
    def __init__(self, settings: Settings, repository: ContentRepository):
        self.settings = settings
        self.repository = repository

    def invoke(self, draft_id: str, draft=None) -> AutoReviewResult:
        """可接收调用方制作的只读快照，避免线程中的数据库 Session 访问。"""
        draft = draft or self.repository.get_draft(draft_id)
        rules = rule_review(
            draft,
            self.settings.draft_body_min_chars,
            self.settings.draft_body_max_chars,
        )
        logger.info(
            "auto_review_rules_checked draft_id=%s passed=%s body_chars=%s paragraph_count=%s failure_count=%s",
            draft_id,
            rules["passed"],
            rules["body_chars"],
            rules["paragraph_count"],
            len(rules["failures"]),
        )
        if not rules["passed"]:
            return AutoReviewResult(
                False,
                rules,
                {"passed": False, "score": 0, "threshold": self.settings.auto_review_pass_score, "skipped": "固定规则未通过"},
            )
        selected_model = model_for(self.settings, "review")
        selected_api_key = api_key_for(self.settings, "review")
        if not (self.settings.llm_enabled and selected_api_key and selected_model):
            return AutoReviewResult(
                False,
                rules,
                {"passed": False, "score": 0, "threshold": self.settings.auto_review_pass_score, "skipped": "LLM 未启用"},
                "AI 审核需要先启用 LLM",
            )
        prompt = (
            "你是资讯文案合规审核器。只基于给定草稿与来源字段，一次性列出所有可观察缺陷。"
            "返回 JSON：score（0-100 整数）、issues（数组，每项有 type、severity、description）、summary。"
            "severity 只能是 critical、major、minor；critical 仅用于缺失来源、无证据事实或可能误导读者的重大问题。"
            f"评分达到 {self.settings.auto_review_pass_score} 分且没有 critical 才能通过。每一项扣分必须对应 issues 中的明确缺陷；"
            "不要保留下一轮再指出的缺陷，不要输出推理过程。"
            "核验：不夸大事实、术语是否按受众需要说明、语言自然、无重复来源链接、适合公众号草稿。"
            + terminology_guidance() + "来源：" + draft.source_name + "；原文：" + draft.source_url
            + "；标题：" + (draft.title_options_json or [""])[0]
            + "；正文：" + draft.body[:6000]
        )
        try:
            logger.info(
                "auto_review_llm_started draft_id=%s model_category=review model=%s",
                draft_id,
                selected_model,
            )
            model = RestrictedContentTaskAgent(self.settings, "review").review(prompt)
            raw_issues = [item.model_dump() for item in model.issues]
            score = _review_score(model.score)
            blocking_issue_count = _blocking_issue_count(raw_issues)
            issues = normalize_review_feedback(raw_issues)
            if score < self.settings.auto_review_pass_score and not issues:
                issues = [f"审核评分 {score} 分，未达到 {self.settings.auto_review_pass_score} 分通过阈值"]
            passed = score >= self.settings.auto_review_pass_score and blocking_issue_count == 0
            return AutoReviewResult(
                passed,
                rules,
                {
                    "passed": passed,
                    "score": score,
                    "threshold": self.settings.auto_review_pass_score,
                    "blocking_issue_count": blocking_issue_count,
                    "issues": issues,
                    "summary": review_feedback_text(model.summary) or "",
                },
            )
        except Exception as exc:
            logger.warning("auto_review_llm_failed error_type=%s", type(exc).__name__)
            message = model_failure_message(exc, "AI 审核", self.settings.content_llm_timeout_seconds)
            return AutoReviewResult(False, rules, {"passed": False, "score": 0, "threshold": self.settings.auto_review_pass_score, "error": message}, message)
