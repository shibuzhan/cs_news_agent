"""自动审核 Tool：规则先行，模型仅输出结构化结论，不返回思维过程。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.agents.content_task_agents import RestrictedContentTaskAgent
from app.config import Settings, api_key_for, model_for
from app.services.model_errors import model_failure_message
from app.services.plain_text import (
    NATURAL_ARTICLE_MIN_CHARS,
    is_source_footer_line,
    normalize_plain_text,
)
from app.services.review_feedback import normalize_review_feedback, review_feedback_text
from app.services.term_policy import terminology_guidance
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.auto_review")


def _preference_rules(repository) -> str:
    """运营者的长期偏好（app_settings）：生成、改稿、审核共用同一套，避免互相打架。"""
    try:
        from app.services.publication_preferences import preference_rules_block

        return preference_rules_block(repository)
    except Exception:
        return ""


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
        item for item in paragraphs if not is_source_footer_line(item.lstrip("　 "))
    )
    if not draft.source_url.startswith(("https://", "http://")):
        failures.append("缺少可追溯原文链接")
    if not draft.source_name or not (draft.title_options_json or []):
        failures.append("缺少来源或标题")
    if not draft.tags_json:
        failures.append("缺少内容标签")
    if body_without_footer and not (min_body_chars <= len(body_without_footer) <= max_body_chars):
        failures.append(
            f"正文长度 {len(body_without_footer)} 不在 {min_body_chars} 到 {max_body_chars} 字符范围内"
        )
    source_kind = str(getattr(getattr(draft, "source_item", None), "source_kind", "")).casefold()
    if source_kind != "github" and body.count("原文标题：") != 1:
        failures.append("原文标题尾注必须且只能出现一次")
    if re.search(r"https?://", body):
        failures.append("正文不应重复放置原文链接")
    if any(marker in body for marker in ("先说结论", "背景介绍：", "实现过程：", "未来展望：")):
        failures.append("正文不应使用模板化显式段落标题")
    content_paragraphs = [item for item in paragraphs if not is_source_footer_line(item.lstrip("　 "))]
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


def _evidence_section(evidence: object, limit: int = 6000) -> str:
    """审核提示词里的来源证据。

    审核模型必须能核对正文里的数字、版本、命令与名称是否出自来源，否则它只能把每一个
    具体细节都判成“缺少来源字段”，改稿就会照着删除，文章越改越空。
    """
    entries = evidence if isinstance(evidence, list) else []
    lines: list[str] = []
    budget = limit
    for entry in entries[:12]:
        if not isinstance(entry, dict):
            continue
        head = " ".join(
            part for part in (
                f"[{entry.get('id')}]" if entry.get("id") else "",
                " ".join(str(entry.get("title") or "").split())[:120],
                " ".join(str(entry.get("url") or "").split())[:120],
            ) if part
        )
        metrics = entry.get("metrics")
        if isinstance(metrics, dict) and metrics:
            head = f"{head} metrics={json.dumps(metrics, ensure_ascii=False)[:300]}"
        text = " ".join(str(entry.get("content") or entry.get("summary") or "").split())
        body = text[: max(budget - len(head), 0)]
        if not body and not head:
            continue
        lines.append(f"{head}\n{body}" if body else head)
        budget -= len(head) + len(body)
        if budget <= 0:
            break
    if not lines:
        return "来源证据：未保存。无法核对时只按常识判断明显无依据的内容，不要仅因无法核对就判为 critical。\n"
    extra = ""
    if any(str(entry.get("id", "")).startswith("search-") for entry in entries if isinstance(entry, dict)):
        extra = "（编号 search-N 的是改稿阶段联网检索到的公开资料，与来源证据同等可用。）\n"
    return "来源证据（正文里的数字、数量、版本、命令、名称与价格，凡在此能找到的都属于有来源，不得判为缺少来源）：\n" + extra + "\n".join(lines) + "\n"


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
            "返回 JSON：score（0-100 整数）、issues（数组，每项有 type、severity、description）、summary、search_queries。"
            "severity 只能是 critical、major、minor；critical 仅用于缺失来源、无证据事实或可能误导读者的重大问题。"
            "search_queries 供改稿环节联网补充使用：只有当某个外部名称**不解释就读不懂本文主体**时才填，"
            "例如文章核心讲的那个项目、方法或平台；"
            "仅仅出现在“支持/兼容/也可用于”这类列举里的工具名不要填，读者不需要靠它理解文章。"
            "最多 1 到 2 条，只写名称本身（不要写成句子或问题）；没有这类名称时返回空数组。"
            f"评分达到 {self.settings.auto_review_pass_score} 分且没有 critical 才能通过。每一项扣分必须对应 issues 中的明确缺陷；"
            "不要保留下一轮再指出的缺陷，不要输出推理过程。"
            "打分锚点：事实准确、术语够用、语言自然、可以直接发布＝85 到 89 分；"
            "在此基础上只需极小的措辞改进＝90 分以上；"
            "存在读者会误解、或需要回查来源才能确认的表述＝84 分以下。"
            "不要因为风格偏好给出 90 分以上的差异，也不要在没有明确缺陷时给低分。"
            "核验：不夸大事实、术语是否按受众需要说明、语言自然、无重复来源链接、适合公众号草稿；"
            "开头是否在第一段交代了主体与背景（这是什么、谁做的、为什么值得看）——"
            "**通篇无主语、或直接从“本地运行后…”“安装后…”这类操作场景开头，属于 major 缺陷**。"
            "判定边界：只有把来源未支持的事实写成确定结论（数字、统计口径、人名、许可证、文件路径、版本、时间等）才算缺陷；"
            "来源字段里已有的指标（如 star、fork、score）和平台元数据属于可用证据，不得判为缺少来源。"
            "**下面会给出来源证据原文**：正文里的数字、数量、版本号、命令、包名、平台清单与价格，凡能在来源证据里找到的都属于有来源，"
            "不得判为“缺少来源字段”；只有来源证据里确实没有的具体细节才算缺陷。"
            "这类“来源里没有的具体细节”按 major 计，不要动辄给 critical；只有会误导读者、或把来源未支持的能力与结论写成既定事实时才用 critical。"
            "正文末尾的“点击查看原文跳转项目地址”由服务端自动添加，不属于草稿内容，不得作为缺陷或要求删除。"
            "通用背景、行业常见现象的概括，或读者可自行判断的表述，不得因为“缺少逐句来源”或“可以更谨慎”而扣分；"
            "不得要求加入“如果放在……语境里看”“从某种角度看”这类套话，也不得为了保守而要求把自然流畅的句子改得生硬。"
            "语言自然、直接优先于措辞谨慎。"
            "范围限定：不得因为文章“没做竞品对比”“没给未来预测”“没写作者观点”“没覆盖其他场景”而扣分；"
            "也不得把正文里已经写过的信息当作“建议补充”。"
            "本项目的文章只聚焦 2 到 3 个要点，因此**不得因为文章没有覆盖安装步骤、价格与套餐、官方渠道清单、"
            "支持语言列表、命令与版本号而扣分或要求补充**——写进这些内容反而是缺陷。"
            "不扣字眼（重要）：**同义改写、近义表达、句子结构变化都不算缺陷**；"
            "把来源里的列举做同类补充（例如来源写“飞机、船舶”，正文写成“飞机、船舶、数据中心”）只要不误导读者就不算缺陷；"
            "措辞偏好、术语选择、语气、句序都不作为缺陷，只有**会让读者对事实产生错误理解**、"
            "或把来源未支持的能力与结论写成既定事实，才算缺陷。"
            "拿不准是否误导时，不要报为缺陷。minor 只保留确实影响阅读的问题，且一次**最多 3 条**。"
            "同一篇文章在多轮审核之间不要反复改变对同一处的判定。"
            "每条 description 必须可直接执行：写明是哪一段的什么问题、应该怎么改（例如“第 3 段的 41987 star 未说明统计口径，"
            "改为‘累计约 4.2 万 star’并注明来源为项目页面”），不要只描述现象或给笼统评价。"
            + terminology_guidance() + _preference_rules(self.repository)
            + "来源：" + draft.source_name + "；原文：" + draft.source_url
            + "；标题：" + (draft.title_options_json or [""])[0] + "\n"
            # 审核必须看到与写作**同一份**证据：此前限制 6000 字符，导致 6000 字之后的
            # 事实（密钥、数量、平台细节）全被判成“来源证据中未出现”。
            + _evidence_section(
                getattr(draft, "evidence_json", None),
                limit=getattr(self.settings, "llm_evidence_max_chars", 20_000),
            )
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
                    # 交给改稿环节联网补充；不改写草稿、不额外增加模型请求。
                    "search_queries": list(model.search_queries),
                },
            )
        except Exception as exc:
            logger.warning("auto_review_llm_failed error_type=%s", type(exc).__name__)
            message = model_failure_message(exc, "AI 审核", self.settings.content_llm_timeout_seconds)
            return AutoReviewResult(False, rules, {"passed": False, "score": 0, "threshold": self.settings.auto_review_pass_score, "error": message}, message)
