from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Protocol

from langsmith.wrappers import wrap_openai
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, Field, ValidationError

from app.agents.content_task_agents import RestrictedContentTaskAgent
from app.config import Settings, api_key_for, base_url_for, model_for
from app.domain.models import DraftContent, NormalizedItem, SourceKind
from app.observability import langsmith_enabled
from app.services.plain_text import (
    NATURAL_ARTICLE_MIN_CHARS,
    compose_natural_article,
    format_source_body,
    normalize_wechat_description,
)
from app.services.term_policy import terminology_guidance
from app.tools.search_tools import ExaMcpSearchError, ExaMcpSearchTool


logger = logging.getLogger("news_agent.generator")

_SOURCE_WRITING_SKILLS = {
    SourceKind.ARXIV: "arxiv-content-writing",
    SourceKind.GITHUB: "github-content-writing",
    SourceKind.HACKER_NEWS: "hacker-news-content-writing",
    SourceKind.RSS: "rss-content-writing",
}


class DraftGenerator(Protocol):
    def generate(self, item: NormalizedItem) -> DraftContent: ...


class GenerationError(RuntimeError):
    pass


def _natural_article_instruction(min_chars: int, max_chars: int) -> str:
    minimum = max(min_chars, NATURAL_ARTICLE_MIN_CHARS)
    return (
        "正文必须返回 body：一个纯文本字符串，使用 4 到 8 个自然段，并以空行分隔。"
        "文章应自然覆盖背景与切入、技术或过程、价值与边界、后续观察这四类信息，但不必一一对应、"
        "也不要写出这些名称、任何小标题、编号或“先说结论”。正文建议控制在 "
        f"{minimum} 到 {max_chars} 个中文字符，且不得少于 {NATURAL_ARTICLE_MIN_CHARS} 个中文字符；"
        "使用自然、生活化的语言，不得大段复制 README 或原文。"
        "每段单独成行，服务端会统一处理段首缩进。"
    )


def _apply_article_body(payload: dict, item: NormalizedItem, min_chars: int = NATURAL_ARTICLE_MIN_CHARS) -> dict[str, int]:
    """接受自然正文；兼容历史模型仍返回的 body_sections。"""
    raw_body = payload.pop("body", None)
    legacy_sections = payload.pop("body_sections", None)
    if not isinstance(raw_body, str) and isinstance(legacy_sections, list):
        raw_body = "\n\n".join(str(section) for section in legacy_sections if isinstance(section, str))
    body, shape = compose_natural_article(raw_body, min_chars=max(min_chars, NATURAL_ARTICLE_MIN_CHARS))
    payload["body"] = format_source_body(body, item.title, item.source_kind.value)
    return shape


def _github_project_name(item: NormalizedItem) -> str:
    return (item.external_id or item.title).strip()


def _title_subject(item: NormalizedItem) -> str:
    return _github_project_name(item) if item.source_kind == SourceKind.GITHUB else item.title


def _ensure_source_title_options(payload: dict, item: NormalizedItem) -> None:
    """GitHub 文章标题必须可见地包含项目名，避免模型只写泛化卖点。"""
    if item.source_kind != SourceKind.GITHUB or not isinstance(payload.get("title_options"), list):
        return
    project_name = _github_project_name(item)
    required = project_name.casefold()
    titles: list[str] = []
    for value in payload["title_options"]:
        if not isinstance(value, str) or not value.strip():
            continue
        title = value.strip()
        titles.append(title if required in title.casefold() else f"{project_name}｜{title}")
    payload["title_options"] = titles


def _source_writing_skill(item: NormalizedItem) -> str:
    """读取项目内来源写作 Skill 及其明确路由的参考资料。"""
    folder = _SOURCE_WRITING_SKILLS.get(item.source_kind)
    if not folder:
        return "遵循通用科技资讯事实约束。"
    skill_root = Path(__file__).resolve().parents[2] / "agent_skills" / folder
    entry_path = skill_root / "SKILL.md"
    reference_path = skill_root / "references" / "evidence-and-search.md"
    try:
        entry = entry_path.read_text(encoding="utf-8").strip()
        reference = reference_path.read_text(encoding="utf-8").strip()
        return f"{entry}\n\n## 来源专用参考资料\n{reference}"
    except OSError:
        logger.warning("source_writing_skill_unavailable source=%s skill=%s", item.source_kind.value, folder)
        return "遵循通用科技资讯事实约束。"


def _is_recoverable_provider_error(exc: Exception) -> bool:
    """识别可向用户说明的模型服务故障；不生成确定性替代文案。"""
    if isinstance(exc, (APIConnectionError, APITimeoutError, TimeoutError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 402 or exc.status_code == 408 or exc.status_code == 429 or exc.status_code >= 500
    return False


class SearchDecision(BaseModel):
    """模型只描述检索需求；是否访问网络由受控 Tool 决定。"""

    need_search: bool = False
    queries: list[str] = Field(default_factory=list, max_length=2)
    reason: str = ""


class DeterministicDraftGenerator:
    """无密钥环境的可测试降级实现，不冒充真实 LLM 翻译。"""

    def generate(self, item: NormalizedItem) -> DraftContent:
        source_summary = item.summary or item.content or item.title
        title = _title_subject(item)[:80]
        evidence_pack = self._evidence_pack(item)
        return DraftContent(
            title_options=[
                f"今日科技资讯｜{title}",
                f"值得关注：{title}",
                f"{item.category.value}速览｜{title}",
            ],
            summary_cn=normalize_wechat_description(
                f"{title}：这项技术动态为什么值得继续关注？"
            ),
            body=format_source_body(
                f"{source_summary[:500]}\n\n"
                "这条资讯聚焦的，是技术从概念走向真实使用时会遇到的问题。对普通读者而言，关键不只是名字新不新，而是它解决了什么实际麻烦。\n\n"
                f"根据来源信息，相关团队围绕“{title}”给出了具体思路与结果。阅读时可以重点关注它采用的方式、适用边界，以及证据是否足以支撑结论。\n\n"
                "它是否会真正改变日常使用体验，还需要更多公开验证。不过，这类尝试至少提供了一个值得持续观察的方向。以上内容根据原始信息整理，发布前请人工核对。",
                item.title,
                item.source_kind.value,
            ),
            tags=[item.category.value, "科技资讯", item.source_kind.value],
            card_script=[
                f"封面：{title}",
                f"发生了什么：{source_summary[:220]}",
                "为什么值得关注：请审核人员结合原文补充判断",
                f"来源：{item.source_name}｜{item.url}",
            ],
            source_name=item.source_name,
            source_url=item.url,
            content_plan={"audience": "科技资讯读者", "angle": "来源事实速览", "risks": ["未调用 LLM，需人工补充解读"]},
            quality_report={"status": "manual_review", "score": 0, "risks": ["确定性降级文案"], "suggestions": ["发布前补充人工解读"]},
            claim_citations=[{"claim": source_summary[:120], "evidence_ids": [evidence_pack[0]["id"]]}],
            evidence_pack=evidence_pack,
            generation_mode="deterministic",
        )

    @staticmethod
    def _evidence_pack(item: NormalizedItem) -> list[dict]:
        aggregate = item.metadata.get("evidence")
        if isinstance(aggregate, list) and aggregate:
            return [dict(entry, id=f"evidence-{index}") for index, entry in enumerate(aggregate, start=1)]
        return [{"id": "source-1", "title": item.title, "url": str(item.url), "summary": item.summary, "content": item.content[:12000]}]


class ResilientDraftGenerator:
    """将模型服务故障转为可审计的生成失败，不伪造模型文案。"""

    def __init__(self, primary: DraftGenerator):
        self.primary = primary

    def generate(self, item: NormalizedItem) -> DraftContent:
        try:
            return self.primary.generate(item)
        except Exception as exc:
            if not _is_recoverable_provider_error(exc):
                raise
            status_code = getattr(exc, "status_code", None)
            logger.warning(
                "draft_generation_provider_failed source=%s external_id=%s error_type=%s status_code=%s",
                item.source_kind.value,
                item.external_id,
                type(exc).__name__,
                status_code,
            )
            if isinstance(exc, (APITimeoutError, TimeoutError, asyncio.TimeoutError)):
                reason = "内容模型请求超时，未在等待时限内收到响应"
            elif isinstance(exc, APIConnectionError):
                reason = "内容模型连接失败，请检查代理或网络连接"
            elif status_code is not None:
                reason = f"内容模型服务返回 HTTP {status_code}"
            else:
                reason = "内容模型服务暂时不可用"
            raise GenerationError(reason) from exc


class OpenAICompatibleDraftGenerator:
    def __init__(self, settings: Settings):
        selected_model = model_for(settings, "content")
        selected_api_key = api_key_for(settings, "content")
        if not selected_api_key or not selected_model:
            raise ValueError("OPENAI_API_KEY 和 LLM_MODEL 均不能为空")
        self.model = selected_model
        self.settings = settings
        self.writer = RestrictedContentTaskAgent(settings, "content")

    def generate(self, item: NormalizedItem) -> DraftContent:
        logger.info(
            "draft_generation_started source=%s external_id=%s mode=baseline model_category=content model=%s",
            item.source_kind.value,
            item.external_id,
            self.model,
        )
        facts = {
            "title": item.title,
            "summary": item.summary,
            "content": item.content[: self.settings.llm_evidence_max_chars],
            "category": item.category.value,
            "source_name": item.source_name,
            "source_url": str(item.url),
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "metrics": item.metrics,
        }
        prompt = (
            "你是科技资讯编辑。只能使用下面 JSON 中的事实生成中文待审核草稿，"
            "不得补充未经来源支持的数字、时间、人物或结论。正文 body 必须像面向普通科技读者的自然讲解，"
            + _natural_article_instruction(self.settings.draft_body_min_chars, self.settings.draft_body_max_chars)
            + terminology_guidance()
            + "来源专用写作规则：\n"
            + _source_writing_skill(item)
            + "\n"
            "body 中只能是纯文本：不得使用 Markdown、HTML、列表符号、图片链接、原文标题或原文链接；来源标题由服务端统一添加，链接由公众号“阅读原文”承载。"
            "summary_cn 是供公众号 description 使用的一句话点击导语，30 到 60 个中文字符、不得换行、不得夸大或虚构。"
            "输出 JSON，字段为：title_options（最多 3 项）、summary_cn、body、tags、card_script、claim_citations。"
            "claim_citations 为数组，每项含 claim 和 evidence_ids；所有 evidence_ids 必须来自来源证据。\n"
            f"事实：{json.dumps(facts, ensure_ascii=False)}"
        )
        try:
            payload = self.writer.write(prompt).model_dump()
            payload["source_name"] = item.source_name
            payload["source_url"] = str(item.url)
            payload["summary_cn"] = normalize_wechat_description(str(payload.get("summary_cn", "")))
            _ensure_source_title_options(payload, item)
            payload["content_plan"] = {
                "article_shape": _apply_article_body(
                    payload,
                    item,
                    self.settings.draft_body_min_chars,
                )
            }
            payload["evidence_pack"] = self._evidence_pack(item)
            draft = DraftContent.model_validate(payload)
            logger.info(
                "draft_generation_finished source=%s external_id=%s body_length=%s paragraph_count=%s",
                item.source_kind.value,
                item.external_id,
                len(draft.body),
                draft.content_plan.get("article_shape", {}).get("paragraph_count"),
            )
            return draft
        except (ValueError, KeyError, json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "draft_generation_invalid source=%s external_id=%s error_type=%s",
                item.source_kind.value,
                item.external_id,
                type(exc).__name__,
            )
            raise GenerationError(f"LLM 输出不符合草稿结构：{exc}") from exc

    def _evidence_pack(self, item: NormalizedItem) -> list[dict]:
        return [{"id": "source-1", "title": item.title, "url": str(item.url), "summary": item.summary, "content": item.content[: self.settings.llm_evidence_max_chars]}]


class EnhancedDraftGenerator(OpenAICompatibleDraftGenerator):
    """证据包、选题规划、写作和质检分离；模型不能自行补充来源事实。"""

    def __init__(self, settings: Settings, search_tool: ExaMcpSearchTool | None = None):
        super().__init__(settings)
        selected_api_key = api_key_for(settings, "content")
        self.client = OpenAI(
            api_key=selected_api_key,
            base_url=base_url_for(settings, "content") or None,
            max_retries=settings.content_llm_max_retries,
            timeout=settings.content_llm_timeout_seconds,
        )
        self.client = wrap_openai(self.client) if langsmith_enabled(settings) else self.client
        self.search_tool = search_tool or ExaMcpSearchTool(settings)

    def _json(self, model: str, prompt: str) -> dict:
        response = self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        if not isinstance(payload, dict):
            raise GenerationError("LLM 输出不是 JSON 对象")
        return payload

    def _evidence(self, item: NormalizedItem) -> list[dict]:
        aggregate = item.metadata.get("evidence")
        if isinstance(aggregate, list) and aggregate:
            return [dict(entry, id=f"evidence-{index}") for index, entry in enumerate(aggregate, start=1)]
        return [{"id": "source-1", "title": item.title, "url": str(item.url), "summary": item.summary, "content": item.content[: self.settings.llm_evidence_max_chars], "metrics": item.metrics}]

    def _search_evidence(self, queries: list[str]) -> list[dict]:
        try:
            return asyncio.run(self.search_tool.search(queries))
        except ExaMcpSearchError:
            logger.warning("draft_generation_search_skipped query_count=%s", len(queries))
            return []

    def generate(self, item: NormalizedItem) -> DraftContent:
        logger.info(
            "draft_generation_started source=%s external_id=%s mode=enhanced model_category=content model=%s",
            item.source_kind.value,
            item.external_id,
            self.model,
        )
        evidence = self._evidence(item)
        fast_model = self.settings.llm_fast_model or self.model
        reasoning_model = self.settings.llm_reasoning_model or self.model
        search_decision = SearchDecision()
        search_evidence: list[dict] = []
        try:
            if self.search_tool.enabled:
                decision_payload = self._json(
                    fast_model,
                    "你是科技资讯检索规划器。只根据来源证据判断：是否需要补充读者理解所需的背景、技术优点、部署或使用语境，"
                    "或来源未解释的专有名词。只有确有必要补充且可获得公开可靠资料时才设置 need_search=true；"
                    "最多给出 2 条自然语言检索词，不得检索个人隐私、凭据或与选题无关的信息。"
                    "输出 JSON：need_search、queries、reason。\n证据包："
                    + json.dumps({"evidence": evidence}, ensure_ascii=False),
                )
                search_decision = SearchDecision.model_validate(decision_payload)
                if search_decision.need_search:
                    search_evidence = self._search_evidence(search_decision.queries)
            facts = {"category": item.category.value, "source_name": item.source_name, "published_at": item.published_at.isoformat() if item.published_at else None, "evidence": [*evidence, *search_evidence]}
            plan = self._json(fast_model, "你是科技资讯选题编辑。只能使用证据包，输出 JSON：audience、angle、outline(数组)、risks(数组)。不得写未被证据支持的事实。\n证据包：" + json.dumps(facts, ensure_ascii=False))
            writer_prompt = (
                "你是科技资讯写作者。只能使用证据包和选题规划。"
                + _natural_article_instruction(self.settings.draft_body_min_chars, self.settings.draft_body_max_chars)
                + terminology_guidance()
                + "联网补充证据只可用于背景、技术优点、部署或使用语境的准确说明，不能补造项目事实。\n来源专用写作规则：\n"
                + _source_writing_skill(item)
                + "\nbody 必须为纯文本，不得使用 Markdown、HTML、列表符号、图片链接、原文标题或原文链接；来源标题由服务端统一添加，链接由公众号“阅读原文”承载。"
                "summary_cn 是公众号 description：一句吸引点击但不夸张的导语，30 到 60 个中文字符、不得换行。"
                "输出 JSON：title_options、summary_cn、body、tags、card_script、claim_citations。claim_citations 为数组，每项含 claim 和 evidence_ids；"
                "所有 evidence_ids 必须来自证据包。不得虚构数字、时间、人物或结论。\n证据包："
                + json.dumps(facts, ensure_ascii=False)
                + "\n规划："
                + json.dumps(plan, ensure_ascii=False)
            )
            writer = (
                self.writer
                if reasoning_model == self.model
                else RestrictedContentTaskAgent(
                    self.settings,
                    "content",
                    model_override=reasoning_model,
                )
            )
            payload = writer.write(writer_prompt).model_dump()
            citations = payload.get("claim_citations", [])
            allowed_ids = {entry["id"] for entry in facts["evidence"]}
            if not isinstance(citations, list) or any(
                not isinstance(entry, dict)
                or not isinstance(entry.get("evidence_ids"), list)
                or not set(entry["evidence_ids"]).issubset(allowed_ids)
                for entry in citations
            ):
                raise GenerationError("草稿包含不存在的证据引用")
            quality = self._json(fast_model, "你是内容质检员。只检查草稿是否被证据支持，不改写草稿。输出 JSON：status(manual_review/high_risk)、score(0-100)、risks(数组)、suggestions(数组)。\n证据包：" + json.dumps(facts, ensure_ascii=False) + "\n草稿：" + json.dumps(payload, ensure_ascii=False))
            if isinstance(payload.get("card_script"), str):
                payload["card_script"] = [payload["card_script"]]
            if isinstance(payload.get("tags"), str):
                payload["tags"] = [tag.strip("# ") for tag in payload["tags"].split() if tag]
            plan["search"] = {"requested": search_decision.need_search, "query_count": len(search_decision.queries), "evidence_count": len(search_evidence), "reason": search_decision.reason}
            _ensure_source_title_options(payload, item)
            plan["article_shape"] = _apply_article_body(
                payload,
                item,
                self.settings.draft_body_min_chars,
            )
            payload.update({"source_name": item.source_name, "source_url": str(item.url), "content_plan": plan, "quality_report": quality, "claim_citations": citations, "evidence_pack": facts["evidence"], "generation_mode": "enhanced"})
            payload["summary_cn"] = normalize_wechat_description(str(payload.get("summary_cn", "")))
            draft = DraftContent.model_validate(payload)
            logger.info(
                "draft_generation_finished source=%s external_id=%s body_length=%s paragraph_count=%s",
                item.source_kind.value,
                item.external_id,
                len(draft.body),
                draft.content_plan.get("article_shape", {}).get("paragraph_count"),
            )
            return draft
        except (ValueError, KeyError, json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "draft_generation_invalid source=%s external_id=%s error_type=%s",
                item.source_kind.value,
                item.external_id,
                type(exc).__name__,
            )
            raise GenerationError(f"增强生成输出不符合结构：{exc}") from exc


def build_generator(settings: Settings) -> DraftGenerator:
    if settings.llm_enabled and api_key_for(settings, "content") and model_for(settings, "content"):
        if settings.enhanced_generation_enabled:
            return ResilientDraftGenerator(EnhancedDraftGenerator(settings))
        return ResilientDraftGenerator(OpenAICompatibleDraftGenerator(settings))
    return DeterministicDraftGenerator()
