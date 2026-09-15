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
    MIN_HARD_BODY_CHARS,
    article_length_band,
    body_char_count,
    body_source_lines_removed,
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


def _version_origin(draft) -> str:
    """这一版正文是怎么来的：按意见改稿 / 按来源重写 / 首次生成。

    记录页要据此标注“版本 N（按来源重写）58 分”——否则用户看到的只是分数在跳，
    会以为“改稿越改越差”，而实际可能是一次整篇重写换了另一份文字。
    """
    plan = getattr(draft, "content_plan_json", None) or {}
    reason = plan.get("last_revision_reason") if isinstance(plan, dict) else None
    if isinstance(reason, dict):
        if reason.get("kind") == "source_regeneration":
            return "source_regeneration"
        if reason.get("issues"):
            return "review_revision"
    return "initial"


def _history_section(repository, draft_id: str, *, current_version: int) -> str:
    """审核提示词里的“历史参照”：上次评分 + 这一版是怎么来的。

    为什么需要：审核只看当前正文、看不到上一版，于是 88 → 74 → 58 这种跨来源比较会让人误以为
    “改稿越改越差”。把版本来源（首次生成 / 按意见改稿 / 按来源重写）与上次分数写进提示词，
    模型才能给出“相对这次输入是否变好”的判断，而不是各打各的分。
    """
    try:
        runs = repository.list_auto_review_runs(draft_id)
    except Exception:  # 审核不该因为读历史失败而中断
        return ""
    if not runs:
        return "本次是这篇稿子的第一次审核（没有历史参照）。\n"
    latest = runs[0]
    report = latest.model_report_json or {}
    score = report.get("score")
    reviewed = report.get("reviewed_version")
    lines = [
        "**历史参照**：上一次审核"
        + (f"在第 {reviewed} 版" if reviewed else "")
        + f"，评分 {score}；当前正文是第 {current_version} 版。"
    ]
    if reviewed and current_version and int(reviewed) != int(current_version):
        lines.append(
            "两版之间正文被改动过（可能是按意见改稿，也可能是按来源整篇重写）："
            "请只评价**当前这一版**，不要因为“上一版已经指出过”就重复扣同一处，也不要默认它一定变好或变差。"
        )
    return "".join(lines) + "\n"


def rule_review(draft, min_body_chars: int = MIN_HARD_BODY_CHARS, max_body_chars: int = 2400) -> dict:
    # 这里不能先调用 normalize_plain_text：它会按设计去除段首空白，
    # 而段首两个全角空格正是本项目需要核验的格式规则。
    # 长度一律用 body_char_count()（去尾注 + 去空白），与提示词、成形校验、改稿、汇报同一口径。
    # 传入的应当是**硬区间**（目标带上下各放宽 HARD_BAND_MARGIN 字）；这里只兜一个绝对底线。
    min_body_chars = max(min_body_chars, MIN_HARD_BODY_CHARS)
    body = draft.body.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [item for item in body.split("\n\n") if item.strip()]
    failures: list[str] = []
    body_without_footer = body_source_lines_removed(body)
    if not draft.source_url.startswith(("https://", "http://")):
        failures.append("缺少可追溯原文链接")
    if not draft.source_name or not (draft.title_options_json or []):
        failures.append("缺少来源或标题")
    if not draft.tags_json:
        failures.append("缺少内容标签")
    body_chars = body_char_count(body)
    # 长度按配置页的区间判定，但**只作为提示性说明**（minor），不再让它一票否决：
    # 用户口径（2026-09-15）：字数以配置页为准，但**不因为字数问题重写**——一次生成/改稿要 9–12 分钟。
    # 因此偏短/偏长会出现在意见里（供改稿参考），但不计入 failures、不影响通过与否。
    length_note = ""
    if body_without_footer and not (min_body_chars <= body_chars <= max_body_chars):
        if body_chars < min_body_chars:
            length_note = (
                f"minor｜长度：正文 {body_chars} 字，低于配置下限 {min_body_chars} 字——"
                "能补来源里的新事实就补，**不要为了字数重复已说过的内容**；"
                "这属于可改可不改，服务端不会因为它重写。"
            )
        else:
            length_note = (
                f"minor｜长度：正文 {body_chars} 字，超过配置上限 {max_body_chars} 字——"
                "优先删掉重复和空泛的句子，不要新加内容。"
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
        "body_chars": body_chars,
        "length_note": length_note,
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
        from app.services.runtime_settings import load_runtime_settings
        self.settings = load_runtime_settings(settings)
        self.repository = repository

    def invoke(self, draft_id: str, draft=None) -> AutoReviewResult:
        """可接收调用方制作的只读快照，避免线程中的数据库 Session 访问。"""
        draft = draft or self.repository.get_draft(draft_id)
        # 配置页填的是目标字数：规则审核只拦硬区间（目标上下各放宽 200 字）。
        rules = rule_review(
            draft,
            *article_length_band(
                self.settings.draft_body_min_chars, self.settings.draft_body_max_chars
            )[2:],
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
            "你是资讯文案审核器。只基于给定草稿与来源字段，一次性列出所有可观察缺陷。"
            # 真实反馈：审核过去爱抠细节，而真正要改的是语气与通顺。
            "**本次审核只看两件事**：① **语气**——是不是分享者在讲刚看到的东西，而不是百科词条或研报；"
            "② **通顺**——句子读得下去吗，有没有重复绕圈、指代不清、句子过长或前后不接。"
            "除此之外的具体细节（术语选择、措辞偏好、句序、要不要多补背景、要不要做对比、要不要展开某段）**都不属于本次审核范围**，不要报为缺陷。"
            # 用户口径（2026-09-15）：字数以配置页为准，但**不因为字数重写**（一次 9–12 分钟）。
            "**长度只作提示、不作为缺陷**：正文比配置区间短或长时，用一条 minor 说清“偏短/偏长、建议补事实或删重复”，"
            "并注明“可改可不改”；**不得因为长度给出 major/critical，也不得要求重写整篇**。"
            "返回 JSON：score（0-100 整数）、issues（数组，每项有 type、severity、description）、summary、search_plan。"
            "severity 只能是 critical、major、minor；critical 仅用于缺失来源、无证据事实或可能误导读者的重大问题。"
            "search_plan 供改稿环节联网补充使用：只有当某个外部名称**不解释就读不懂本文主体**时才填，"
            "每项必须包含 subject（具体主体）、need（文章缺失的事实或方法）、reason（为何需要补）、"
            "preferred_source（official_docs / repository / authoritative_web）；例如"
            '{"subject":"openai/plugins","need":"插件开发与配置方式","reason":"正文提到开发入口但未解释","preferred_source":"repository"}。'
            "subject 必须是文章核心项目、仓库或平台标识；"
            "仅仅出现在“支持/兼容/也可用于”这类列举里的工具名不要填，读者不需要靠它理解文章。"
            # 真实反馈（2026-09-15）：审核给出的 `Codex`、`Web` 这种裸词只会搜到官网首页或维基百科。
            "**禁止**把通用词（Web、API、插件、示例这类）或厂商名（OpenAI、Google）作为 subject，"
            "need 也不能写“是什么／背景／用途”这类空泛词；"
            "最多 1 到 2 条；没有这类名称时返回空数组。"
            f"评分达到 {self.settings.auto_review_pass_score} 分且没有 critical 才能通过。每一项扣分必须对应 issues 中的明确缺陷；"
            "不要保留下一轮再指出的缺陷，不要输出推理过程。"
            # 真实问题（2026-09-14）：只有一句“85–89＝可发布”，没有扣分刻度 → 同一篇稿子在不同次审核里
            # 分数差得很大（88 → 74 → 0 → 58），用户无法判断“改稿到底有没有变好”。这里给出可复算的刻度。
            "**扣分刻度（必须按它算分）**：从 100 分起算——每条 critical 扣 25 分、每条 major 扣 8 分、每条 minor 扣 2 分，"
            "最低 20 分；同一处缺陷只扣一次，不要为同一句话既扣语气又扣通顺。"
            "打分锚点：没有任何缺陷、事实准确、语气是分享者口吻、语句通顺、可以直接发布＝85 到 89 分；"
            "在此基础上只需极小的语气或顺句调整＝90 分以上；"
            "存在读者会误解、或需要回查来源才能确认的表述＝84 分以下。"
            "不要因为风格偏好给出 90 分以上的差异，也不要在没有明确缺陷时给低分。"
            "核验：不夸大事实、术语是否按受众需要说明、语气与通顺是否达标、无重复来源链接、适合公众号草稿；"
            "开头是否在第一段交代了主体与背景（这是什么、谁做的、为什么值得看）——"
            "**通篇无主语、或直接从“本地运行后…”“安装后…”这类操作场景开头，属于 major 缺陷**。"
            "判定边界：只有把来源未支持的事实写成确定结论（数字、统计口径、人名、许可证、文件路径、版本、时间等）才算缺陷；"
            "来源字段里已有的指标（如 star、fork、score）和平台元数据属于可用证据，不得判为缺少来源。"
            # 生成端被要求把 41987 写成“约 4.2 万”，审核端必须给同一口径的豁免，否则每次都会被判“来源里没有”。
            "**数字按中文读者习惯改写属于写作规范，不是缺陷**：来源写 41987，正文写“约 4.2 万”，"
            "或是来源写 1234 而正文写“约 1200”，只要量级与来源一致就算有来源，不得判为“来源证据中未出现”或“数字不精确”。"
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
            "措辞偏好、术语选择、句序都不作为缺陷——例外只有两类：**整体语气**与**通顺程度**，见上文审核重点。"
            # 真实反馈：文案语气太过严谨严肃。分享者口吻是目标文体，改稿不得把它改回百科腔。
            "**分享者口吻是目标文体，不是缺陷**：对话感的“你”、口语连接词与短句一律不得扣分；"
            "反过来，**整篇都是百科定义句（“X 是……的一个……”）、研报腔或通篇无主语的客观陈述属于 major 缺陷**"
            "（读者读不下去），可以要求改成分享者口吻；只有整篇如此才算，个别句子偏正式不扣分。"
            # 生成端明令禁止叫卖词与烂梗、且明确不用第一人称；审核端必须同口径。
            "**分享口吻的边界**：感叹号堆叠、“绝了／封神／家人们／重磅／炸裂”这类叫卖词与网络烂梗属于 tone 上的 minor"
            "（一次最多 1 条）；**出现第一人称（“我／我们／笔者／小编”）也属于 tone 上的 minor**（同一处只报一次）；"
            "个别口语词、语气词不算缺陷。"
            "**通顺问题**按同样尺度：整段绕圈重复、指代不明、一句话套三层转折、上下句接不上，属于 major；"
            "个别用词不完美不算。"
            "拿不准是否误导时，不要报为缺陷。minor 只用于语气与通顺上确实影响阅读的问题，且一次**最多 2 条**；不要为细节挑刺。"
            "同一篇文章在多轮审核之间不要反复改变对同一处的判定。"
            "每条 description 必须可直接执行：写明是哪一段的什么问题、应该怎么改，"
            "例如“第 2 段‘对读者来说，这里最有信息量的地方在于……’是元评论，直接写出那件值得看的事”"
            "或“第 4 段两句话在重复同一个意思，删掉后一句”，不要只描述现象或给笼统评价。"
            + terminology_guidance() + _preference_rules(self.repository)
            + "来源：" + draft.source_name + "；原文：" + draft.source_url
            + "；标题：" + (draft.title_options_json or [""])[0] + "\n"
            # 上次审核的分数与这一版的来源：让模型知道“这次是全新生成还是按意见改稿”，
            # 避免把不同来源的版本当成同一篇在反复挑刺（真实困惑：为什么改稿后分数反而降了）。
            + _history_section(self.repository, draft_id, current_version=int(getattr(draft, "version", 0) or 0))
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
            # 长度提示（规则层的偏短/偏长）并入意见：它是“可改可不改”的 minor，不参与通过判定。
            length_note = str(rules.get("length_note") or "")
            if length_note:
                issues = [length_note, *issues]
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
                    "search_plan": [item.model_dump() for item in model.search_plan],
                    "search_queries": list(model.search_queries),
                    # 记录这次审的是第几版、这一版是怎么来的：记录页与下一次审核据此给出可比的说明。
                    "reviewed_version": int(getattr(draft, "version", 0) or 0),
                    "version_origin": _version_origin(draft),
                },
            )
        except Exception as exc:
            logger.warning("auto_review_llm_failed error_type=%s", type(exc).__name__)
            message = model_failure_message(exc, "AI 审核", self.settings.content_llm_timeout_seconds)
            return AutoReviewResult(False, rules, {"passed": False, "score": 0, "threshold": self.settings.auto_review_pass_score, "error": message}, message)
