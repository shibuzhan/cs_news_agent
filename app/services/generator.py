from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from langsmith.wrappers import wrap_openai
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from app.agents.content_task_agents import RestrictedContentTaskAgent
from app.config import Settings, api_key_for, base_url_for, model_for
from app.domain.models import DraftContent, NormalizedItem, SourceKind
from app.observability import langsmith_enabled
from app.paths import agent_skills_dir
from app.services.model_errors import generation_failure_message, is_provider_error
from app.services.evidence_selector import build_evidence
from app.services.plain_text import (
    MIN_HARD_BODY_CHARS,
    NATURAL_ARTICLE_MIN_CHARS,
    article_length_band,
    compose_natural_article,
    format_source_body,
    normalize_wechat_description,
    select_evidence_text,
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


def _run_coroutine(coro):
    """在同步代码里执行协程，并兼容“调用方已经在事件循环里”的情况。

    采集任务通过 `asyncio.to_thread` 调用生成器（独立线程，没有运行中的循环），
    而原记录重生成是在 ARQ 协程里直接调用的——此时 `asyncio.run()` 会抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`。
    因此在检测到运行中的循环时，改在一次性线程里跑它自己的循环。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _natural_article_instruction(target_low: int, target_high: int) -> str:
    """写作指令按「人设 / 结构 / 语言 / 事实 / 输出」分层，避免一整段长约束被模型漏读。

    真实反馈：“现在的文案语气太过严谨和严肃了”。缺一句人设，模型就会退回训练语料里最稳的
    文体——百科词条 + 研报：定义式开头（“X 是……的一个……”）、否定式定义、元评论代替事实、
    段末概括收束句。所以这里**先给身份与口吻**，再给禁用句式与对照示例。
    """
    target_low, target_high, minimum, maximum = article_length_band(target_low, target_high)
    return (
        "\n【人设与语气】你是一个每天都在翻技术资讯的分享者，刚从 GitHub／Hacker News／论文里看到这件事，"
        "正讲给一个懂点技术、但还没听说过它的朋友。写作时保持这个视角：直接对读者说话（“你要是……就会用到”），"
        # 用户明确要求：不要第一人称。
        "**但不许用第一人称**——不写“我”“我们”“笔者”“小编”，也不写“我看到的”“我觉得”；"
        "要表达判断就用直接陈述（“值得看的是……”“麻烦的地方在于……”）。"
        "**每一句判断后面都要跟着来源支持的事实**，不要只给感受不给信息。"
        "分享口吻不等于营销腔：不用感叹号堆叠，不用“绝了／封神／家人们／重磅／炸裂”这类叫卖词，不玩网络烂梗。"
        "\n【不要百科腔与研报腔】禁止“X 是……的一个……”“不是……而是……”“对读者来说……”“这意味着……”"
        "“这也让它成为……的入口”“总的来说”“综上所述”“值得关注的是”这些句式；"
        "也不要每段结尾都补一句概括——如果那句话只是复述本段（例如“这些可选部分可以单独出现，也可以组合出现”），就删掉。"
        "把事说完比给出总结重要。"
        "\n【对照示例】照着右边的写法写，不要照左边："
        "①“openai/plugins 是 OpenAI 在 GitHub 上放出的一个开源项目，主题是一组经过挑选的 Codex 插件示例。”→"
        "“OpenAI 把 Codex 插件该怎么写做成了一组现成样例，放在 openai/plugins 里；想给 Codex 写插件，照着抄一份就行。”；"
        "②“对普通科技读者来说，这个项目值得看的地方在于……”→ 直接把那件值得看的事说出来；"
        "③“两个市场文件都服务于插件查找，但面向不同登录场景。”→"
        "“插件从哪个入口安装，取决于你怎么登录：API key 用户和普通用户各有一个市场文件。”"
        "\n【结构】正文必须返回 body：一个纯文本字符串，使用 4 到 8 个自然段，段落之间以空行分隔。"
        "全文应覆盖背景与切入、技术或过程、价值与边界、后续观察这几类信息，但**不要每篇都套同一个顺序**，"
        "也不要写出这些名称、小标题、编号或“先说结论”；让信息自然长出来，允许出现两三句话的短段，长短交替。"
        "每一段都必须带来来源支持的新信息，不要用空话把段落凑满。"
        "\n【开头】第一段就直接讲你看到了什么：**这是什么（项目/研究/产品名）、谁做的或来自哪里、它想解决什么麻烦**，"
        "用一句人话把主体交代清楚，**不要用“X 是……的一个……”这类百科定义句开头**。"
        "**每个句子都要有明确主语**，不要写“本地运行后，浏览器中会显示……”"
        "“安装后即可看到……”这类没有主语、也不说明在看什么的句子；也不要直接从操作步骤或界面现象开头。"
        "\n【语言】写给普通科技读者：不要“随着……的发展”“在当今……时代”这类万能开头，"
        "结尾也不要“综上所述”“总的来说”“值得关注的是”这种收束套话。每段只说清一件事，避免通篇都是同样长度的中长句。"
        "句子可以短，也可以用口语连接词（说白了、麻烦的地方在于、有意思的是），但不要连续堆口语词。"
        "禁止“如果把它放在……的语境里看”“从某种角度看”“在一定程度上”这类翻译腔或空泛铺垫，能直接说清楚的就直接说。"
        "数字按中文读者习惯写（41987 写成约 4.2 万），单位与量级要与来源一致。"
        "\n【取材与重点】先选**2 到 3 个要点**，每点用 1 到 2 段展开，其余信息一句话带过即可，不要逐条覆盖来源："
        "第一点通常是它想解决什么问题、为什么会出现；第二点是它具体能做什么、怎么做到；第三点是普通人怎么用起来、边界在哪里。"
        "**安装步骤、价格与套餐、官方渠道清单、支持语言列表、命令与参数、版本号一律不写**"
        "（除非本篇主体就是某个版本的发布公告），这些内容对读者没有价值，也会让文章变成说明书的摘要。"
        "读者要了解的是这个项目或事件本身：重点写背景（为什么会有它）、能力（它具体能做什么）、用法与适用场景（谁在什么情况下怎么用）。"
        "不要出现来源文件名（例如 README、仓库摘要、项目简介），也不要把它们当成叙述的主语，直接陈述事实即可。"
        "正文提到的外部产品、工具、平台或组织名，第一次出现时用半句话自然交代它是什么"
        "（例如“一个命令行里的编码 agent”），不要用括号堆注解；来源与检索都没有说明的就不展开。"
        "\n【禁止凑数】不要用“来源没有说明”“没有给出”“无法确认”“只能说明”这类句子充当内容，也不要把同一层意思换句话重复；"
        "正文里每一段都必须带来新的、来源支持的信息。如果证据只够写几点，就把这几点写清楚、写具体，不要为了篇幅反复议论来源缺了什么。"
        # 实测：新口吻解决了百科腔，但模型改用“列举→逐条复述→总括”来凑字数，审核连报 6 条重复。
        "**尤其禁止这种凑字数的写法**：先把几个例子列一遍，再逐个复述一遍，最后补一句总括"
        "（“这些示例共同说明……”“这也说明……”“这些插件包让仓库的示例类型更完整”）。"
        "每个要点只讲一次：列举之后直接给判断或结论，不要回头复述；段落结尾不要写只为收尾而存在的句子，"
        "如果一段的最后一句能删掉而信息不减，就删掉它。"
        "\n【事实】只能使用给定的证据包：来源未支持的数字、时间、人物或结论不要写；确需提及就用来源事实直接表述，"
        "不要用含糊措辞掩盖，也不要大段复制 README 或原文。"
        # 实测：模型把采集/榜单时间写成文章日期，还描述了来源里不存在的界面文字。
        "**采集时间、榜单抓取时间、当前日期都不是来源事实**，不要写成文章里的日期或“榜单页面给出的时间”；"
        "**不要描述来源里没有的界面文字**（例如“仓库标题只写了……”“页面上写着……”）；"
        "仓库名、项目名、文件名、清单名一律原样引用，不改写、不翻译、不缩写。"
        "\n【输出】正文建议控制在 "
        f"{target_low} 到 {target_high} 个字（硬性要求：不得少于 {minimum} 个字、不得超过 {maximum} 个字；"
        "这里数的是**去掉空白与来源尾注后的全部字符**：汉字、英文字母、数字、标点各算一个，"
        "因此英文名与代码片段较多的稿子要相应少写几段），"
        "不要贴着下限写，也不要写到接近上限——超过上限会被规则审核直接判定为不合格。"
        "每段单独成行，服务端会统一处理段首缩进。"
    )


def _hard_body_minimum(settings) -> int:  # noqa: ANN001 - Settings 的测试替身同形
    """成形与规则审核使用**硬下限**：目标下限再放宽 HARD_BAND_MARGIN 字。

    正文短于目标带是写作问题（提示词负责），短于硬下限才是硬性不合格。
    """
    return article_length_band(
        getattr(settings, "draft_body_min_chars", NATURAL_ARTICLE_MIN_CHARS),
        getattr(settings, "draft_body_max_chars", NATURAL_ARTICLE_MIN_CHARS + 600),
    )[2]


def _apply_article_body(payload: dict, item: NormalizedItem, min_chars: int = MIN_HARD_BODY_CHARS) -> dict[str, int]:
    """接受自然正文；兼容历史模型仍返回的 body_sections。"""
    raw_body = payload.pop("body", None)
    legacy_sections = payload.pop("body_sections", None)
    if not isinstance(raw_body, str) and isinstance(legacy_sections, list):
        raw_body = "\n\n".join(str(section) for section in legacy_sections if isinstance(section, str))
    body, shape = compose_natural_article(raw_body, min_chars=max(min_chars, MIN_HARD_BODY_CHARS))
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
    skills_dir = agent_skills_dir()
    if skills_dir is None:
        logger.warning(
            "source_writing_skill_unavailable source=%s skill=%s reason=asset_root",
            item.source_kind.value,
            folder,
        )
        return "遵循通用科技资讯事实约束。"
    skill_root = skills_dir / folder
    entry_path = skill_root / "SKILL.md"
    reference_path = skill_root / "references" / "evidence-and-search.md"
    try:
        entry = entry_path.read_text(encoding="utf-8").strip()
        reference = reference_path.read_text(encoding="utf-8").strip()
        return f"{entry}\n\n## 来源专用参考资料\n{reference}"
    except OSError:
        logger.warning(
            "source_writing_skill_unavailable source=%s skill=%s reason=unreadable root=%s",
            item.source_kind.value,
            folder,
            skills_dir,
        )
        return "遵循通用科技资讯事实约束。"


def _is_recoverable_provider_error(exc: Exception) -> bool:
    """兼容旧调用方；所有供应商错误现在统一脱敏为可操作原因。"""
    return is_provider_error(exc)


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
        return [
            {
                "id": "source-1",
                "title": item.title,
                "url": str(item.url),
                "summary": item.summary,
                "content": item.content[:12000],
                # 指标与来源字段必须一并进入证据包，否则审核模型无法核对正文引用的数字。
                "source_name": item.source_name,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "metrics": item.metrics,
            }
        ]


class ResilientDraftGenerator:
    """将模型服务故障转为可审计的生成失败，不伪造文案，也不回显供应商原始响应。"""

    def __init__(self, primary: DraftGenerator):
        self.primary = primary

    def generate(self, item: NormalizedItem) -> DraftContent:
        try:
            return self.primary.generate(item)
        except GenerationError:
            raise
        except Exception as exc:
            if not is_provider_error(exc):
                # 非供应商错误（例如真实代码缺陷）保持原样，由上层显示通用失败原因。
                raise
            timeout_seconds = getattr(
                getattr(self.primary, "settings", None), "content_llm_timeout_seconds", None
            )
            reason = generation_failure_message(exc, timeout_seconds)
            logger.warning(
                "draft_generation_provider_failed source=%s external_id=%s error_type=%s status_code=%s reason=%s",
                item.source_kind.value,
                item.external_id,
                type(exc).__name__,
                getattr(exc, "status_code", None),
                reason,
            )
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
        # 由 ContentPipeline 在持有数据库会话后注入；直接离线调用仍可读取 style.md。
        self.repository = None

    def _preference_rules(self) -> str:
        """读取长期写作偏好，不让偏好存储故障阻断正文生成。"""
        try:
            from app.services.publication_preferences import preference_rules_block

            return preference_rules_block(self.repository)
        except Exception as exc:  # 偏好读取失败只降级为无偏好，不伪造规则。
            logger.warning(
                "publication_preferences_unavailable model=%s error_type=%s",
                self.model,
                type(exc).__name__,
            )
            return ""

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
            "content": build_evidence(
                self.settings, item.content, self.settings.llm_evidence_max_chars, item.external_id
            ),
            "category": item.category.value,
            "source_name": item.source_name,
            "source_url": str(item.url),
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "metrics": item.metrics,
        }
        prompt = (
            "你是一个每天都在翻 GitHub、Hacker News 与论文的资讯分享者，看到有意思的东西就写下来讲给读者；"
            "不是编辑、不是分析师、也不是产品说明书。用分享者的第一人称口吻写作，但只能使用下面 JSON 中的事实，"
            "不得补充未经来源支持的数字、时间、人物或结论。"
            + _natural_article_instruction(self.settings.draft_body_min_chars, self.settings.draft_body_max_chars)
            + terminology_guidance()
            + self._preference_rules()
            + "来源专用写作规则：\n"
            + _source_writing_skill(item)
            + "\n"
            "body 中只能是纯文本：不得使用 Markdown、HTML、列表符号、图片链接、原文标题或原文链接；来源标题由服务端统一添加，链接由公众号“阅读原文”承载。"
            "summary_cn 是供公众号 description 使用的一句话导语，30 到 60 个中文字符、不得换行、不得夸大或虚构；"
            "用分享者的口吻说清“这是什么、为什么值得点开”，不要写成新闻通稿式的一句话概括。"
            "title_options 同样用分享者口吻（可以有判断或悬念），但每一项都必须完整包含项目名或研究主体名。"
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
                    _hard_body_minimum(self.settings),
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
            raise GenerationError("内容模型输出不符合草稿结构，请重试或检查模型配置") from exc

    def _evidence_pack(self, item: NormalizedItem) -> list[dict]:
        return [
            {
                "id": "source-1",
                "title": item.title,
                "url": str(item.url),
                "summary": item.summary,
                "content": build_evidence(
                    self.settings, item.content, self.settings.llm_evidence_max_chars, item.external_id
                ),
                # 指标与来源字段必须一并进入证据包，否则审核模型无法核对正文引用的数字。
                "source_name": item.source_name,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "metrics": item.metrics,
            }
        ]


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
        return [{"id": "source-1", "title": item.title, "url": str(item.url), "summary": item.summary, "content": select_evidence_text(item.content, self.settings.llm_evidence_max_chars), "metrics": item.metrics}]

    def _search_evidence(self, queries: list[str]) -> list[dict]:
        try:
            return _run_coroutine(self.search_tool.search(queries))
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
                    "你是科技资讯检索规划器。只根据来源证据判断：是否需要补充读者理解所需的背景、技术优点、使用或部署语境，"
                    "或来源未解释的专有名词。"
                    "**正文会提到来源里的外部产品、工具、平台与组织名**（编码工具、编辑器、厂商、服务等）："
                    "只有当某个名称不解释就读不懂文章主体时才需要检索（例如文章核心讲的项目、方法或平台）；"
                    "仅仅出现在“支持/兼容/也可用于”这类列举里的工具名不必检索。"
                    "需要安装或使用方式时也可检索，但优先补齐与主体相关的名称与背景，而不是补充命令细节。"
                    "只有确有必要补充且可获得公开可靠资料时才设置 need_search=true；"
                    "最多给出 2 条自然语言检索词（优先把最关键的 1 到 2 个名称放进检索词），"
                    "不得检索个人隐私、凭据或与选题无关的信息。"
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
                + "联网补充证据只可用于背景、技术优点、名称与使用语境的准确说明，不能补造项目事实；"
                "如果联网证据说明了某个外部产品、工具或组织的来历，就用半句话写进正文，帮助读者看懂它是什么。\n来源专用写作规则：\n"
                + _source_writing_skill(item)
                + "\nbody 必须为纯文本，不得使用 Markdown、HTML、列表符号、图片链接、原文标题或原文链接；来源标题由服务端统一添加，链接由公众号“阅读原文”承载。"
                "summary_cn 是公众号 description：一句吸引点击但不夸张的导语，30 到 60 个中文字符、不得换行。"
                "输出 JSON：title_options、summary_cn、body、tags、card_script、claim_citations。claim_citations 为数组，每项含 claim 和 evidence_ids；"
                "只对写进正文的具体事实（数字、版本、许可、时间、外部结论）标注，"
                "普通叙述、过渡句与常识性说明不必逐句标注，也不要为了标注而把句子写得不自然。"
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
                _hard_body_minimum(self.settings),
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
            raise GenerationError("增强生成输出不符合结构，请重试或检查模型配置") from exc


def build_generator(settings: Settings) -> DraftGenerator:
    from app.services.runtime_settings import load_runtime_settings
    settings = load_runtime_settings(settings)
    if settings.llm_enabled and api_key_for(settings, "content") and model_for(settings, "content"):
        if settings.enhanced_generation_enabled:
            return ResilientDraftGenerator(EnhancedDraftGenerator(settings))
        return ResilientDraftGenerator(OpenAICompatibleDraftGenerator(settings))
    return DeterministicDraftGenerator()
