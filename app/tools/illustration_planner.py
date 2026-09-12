"""受控配图规划：决定数量、位置与池内主体，不暴露模型推理过程。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from langsmith.wrappers import wrap_openai
from openai import OpenAI

from app.config import Settings, api_key_for, base_url_for, model_for
from app.observability import langsmith_enabled
from app.services.image_brief import ImageBrief, load_brief, pick_object, pick_objects, pick_style, spread_categories
from app.services.plain_text import is_source_footer_line
from app.storage.repositories import ContentRepository
from app.tools.image_generation import GeneratedIllustration, ImageGenerationTool


logger = logging.getLogger("news_agent.illustration_planner")
MAX_AUTO_ILLUSTRATIONS = 3

# 仅当来源写作 Skill 的 `references/image-brief.md` 不可用时才使用的兜底方向（例如未知来源或资源目录
# 缺失）。四条方向只用真实介质与实物，不再回到 isometric 模块图、抽象数据流这类最容易显得像 AI 的构图。
INLINE_VISUAL_DIRECTIONS = (
    "a close-up photograph of one simple physical object on a matte desk, soft directional daylight, shallow depth of field",
    "a documentary photograph of a tidy workspace corner with analog tools, cool window light, no people",
    "a two-colour risograph print of flat geometric shapes, visible paper grain, slight ink misregistration",
    "a still-life photograph of stacked blank paper and one small metal tool under hard side light",
)


def visual_direction_for(purpose: str, placement_after_paragraph: int) -> str:
    """稳定地给异步图片任务分配不同构图，不依赖队列中的临时内存。"""
    if purpose == "cover":
        return (
            "an editorial still-life photograph of one everyday object on a neutral paper backdrop, "
            "hard directional light, generous negative space"
        )
    return INLINE_VISUAL_DIRECTIONS[(max(placement_after_paragraph, 1) - 1) % len(INLINE_VISUAL_DIRECTIONS)]


@dataclass(frozen=True)
class IllustrationPlan:
    placements: list[int]
    mode: str
    # 与 placements 一一对应的实物池条目；池不可用时为空元组。
    subjects: tuple[str, ...] = ()
    # 与 placements 一一对应的风格池条目：同一篇内每张图风格也不同。
    styles: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublicationAssetSelection:
    cover_asset_id: str
    inline_asset_ids: list[str]
    mode: str


class PublicationAssetSelectionError(RuntimeError):
    """投递素材必须由模型确定时的安全失败。"""


def _paragraphs(body: str) -> list[str]:
    """正文自然段：来源尾注与文末提示行不计入，也不参与插图定位。"""
    return [
        item.strip() for item in body.replace("\r", "").split("\n\n")
        if item.strip() and not is_source_footer_line(item.lstrip("　 "))
    ]


def _inline_limit(paragraphs: list[str]) -> int:
    """正文插图的位置上限：最后一段之前，保证文末不出现插图。"""
    return max(len(paragraphs) - 1, 0)


def fallback_plan(
    body: str,
    brief: ImageBrief | None = None,
    draft_id: str | None = None,
) -> IllustrationPlan:
    paragraphs = _paragraphs(body)
    if len(paragraphs) < 2:
        return IllustrationPlan([], "deterministic")
    limit = _inline_limit(paragraphs)
    if len(paragraphs) >= 6:
        candidates = [2, 4, 5]
    elif len(paragraphs) >= 5:
        candidates = [2, 4]
    else:
        candidates = [2]
    placements = [position for position in candidates if position <= limit]
    return IllustrationPlan(
        placements,
        "deterministic",
        pick_objects(brief, draft_id, "inline", placements),
        tuple(pick_style(brief, draft_id, "inline", position) for position in placements),
    )


def subject_for(draft, purpose: str, placement_after_paragraph: int = 1) -> str:
    """按草稿 ID 在来源实物池里轮换一个主体；没有 brief 时返回空串。"""
    brief = load_brief(getattr(getattr(draft, "source_item", None), "source_kind", None))
    return pick_object(brief, getattr(draft, "id", None), purpose, placement_after_paragraph)


def style_for(draft, purpose: str = "cover", placement_after_paragraph: int = 1) -> str:
    """按草稿 ID 在来源风格池里轮换一条风格；没有 brief 时返回空串。"""
    brief = load_brief(getattr(getattr(draft, "source_item", None), "source_kind", None))
    return pick_style(brief, getattr(draft, "id", None), purpose, placement_after_paragraph)


def _rotated_styles(
    brief: ImageBrief | None,
    base_index: object,
    count: int,
    draft_id: str | None = None,
    placements: list[int] | None = None,
) -> tuple[str, ...]:
    """把模型给出的起始风格编号按插图顺序错开，得到每张图各自的风格。

    模型没给有效编号时，用草稿 ID 与第一张图的位置哈希出起点，再逐张错开。
    """
    if brief is None or not brief.styles or count <= 0:
        return tuple("" for _ in range(max(count, 0)))
    if isinstance(base_index, int) and 1 <= base_index <= len(brief.styles):
        start = base_index - 1
    else:
        first_position = placements[0] if placements else 1
        base_style = pick_style(brief, draft_id, "inline", first_position)
        start = brief.styles.index(base_style) if base_style in brief.styles else 0
    return tuple(brief.styles[(start + offset) % len(brief.styles)] for offset in range(count))


def _plan_prompt(brief: ImageBrief | None, paragraphs: list[str], title: str) -> str:
    """把风格池与实物池编号后交给规划模型；模型只返回编号，不能自创池外内容。"""
    article = (
        f"文章标题：{title}；正文段落："
        + json.dumps(paragraphs, ensure_ascii=False)
    )
    if brief is None:
        return (
            "你是资讯文章配图规划器。只返回 JSON：placements（整数数组）。"
            "根据正文结构决定是否需要正文插图、插几张及每张应插在第几段后。"
            f"最多 {MAX_AUTO_ILLUSTRATIONS} 张；没有必要可返回空数组。"
            "禁止封面、品牌、人物、截图或正文以外的信息。不要输出解释或推理。"
            f"{article}"
        )
    styles = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(brief.styles))
    objects = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(brief.objects))
    return (
        "你是资讯文章配图规划器。只返回 JSON："
        '{"style_index":2,"placements":[{"after_paragraph":2,"subject_index":3}]}。'
        "style_index 为整篇文章挑一条统一的画面风格；placements 为每张正文插图决定插在第几段之后，"
        "并从实物清单里挑选最贴合该段内容的一个实物。"
        f"正文插图最多 {MAX_AUTO_ILLUSTRATIONS} 张；没有必要可返回空数组。"
        "两个编号都只能填清单里已有的编号，不得填写编号以外的内容、不得描述颜色或自行发明实物；"
        "不要输出解释或推理。\n"
        f"风格清单：\n{styles}\n"
        f"实物清单：\n{objects}\n"
        f"{article}"
    )


class IllustrationPlanner:
    def __init__(self, settings: Settings):
        self.settings = settings

    def decide(self, draft) -> IllustrationPlan:
        paragraphs = _paragraphs(draft.body)
        brief = load_brief(getattr(getattr(draft, "source_item", None), "source_kind", None))
        draft_id = getattr(draft, "id", None)
        selected_model = model_for(self.settings, "illustration_planner")
        selected_api_key = api_key_for(self.settings, "illustration_planner")
        if not (self.settings.llm_enabled and selected_api_key and selected_model):
            return fallback_plan(draft.body, brief, draft_id)
        prompt = _plan_prompt(brief, paragraphs, (draft.title_options_json or [""])[0])
        try:
            logger.info(
                "illustration_plan_llm_started draft_id=%s model_category=illustration_planner model=%s styles=%s objects=%s",
                getattr(draft, "id", "unknown"),
                selected_model,
                len(brief.styles) if brief else 0,
                len(brief.objects) if brief else 0,
            )
            client = OpenAI(
                api_key=selected_api_key,
                base_url=base_url_for(self.settings, "illustration_planner") or None,
                max_retries=self.settings.content_llm_max_retries,
                timeout=self.settings.content_llm_timeout_seconds,
            )
            client = wrap_openai(client) if langsmith_enabled(self.settings) else client
            response = client.chat.completions.create(
                model=selected_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, temperature=0,
            )
            payload = json.loads(response.choices[0].message.content or "{}")
            # 位置夹在“最后一段之前”，模型给出文末位置时自动前移。
            limit = _inline_limit(paragraphs)
            placements: list[int] = []
            subjects: list[str] = []
            raw_style = payload.get("style_index")
            for item in payload.get("placements", []):
                if isinstance(item, int):
                    position, chosen_index = item, None
                elif isinstance(item, dict):
                    position, chosen_index = item.get("after_paragraph"), item.get("subject_index")
                else:
                    continue
                if not isinstance(position, int) or not 1 <= position <= limit or position in placements:
                    continue
                placements.append(position)
                # 只接受池内编号；越界、缺失或类型不对时按草稿 ID 轮换，模型无法自创实物。
                if brief and isinstance(chosen_index, int) and 1 <= chosen_index <= len(brief.objects):
                    subjects.append(brief.objects[chosen_index - 1])
                else:
                    subjects.append(pick_object(brief, draft_id, "inline", position))
                if len(placements) == MAX_AUTO_ILLUSTRATIONS:
                    break
            # 同一篇内避开同一类实物；风格按模型给的起点逐张错开。
            spread = spread_categories(brief, tuple(subjects), draft_id, "inline", placements)
            return IllustrationPlan(
                placements,
                "llm",
                spread,
                _rotated_styles(brief, raw_style, len(placements), draft_id, placements),
            )
        except Exception as exc:
            logger.warning("illustration_plan_llm_fallback error_type=%s", type(exc).__name__)
            return fallback_plan(draft.body, brief, draft_id)

    def decide_publication_assets(self, draft, illustrations) -> PublicationAssetSelection:
        """只从已绑定的生成图片中选择公众号封面与正文插图，不接受前端逐张勾选。"""
        candidates = [
            {
                "asset_id": item.asset_id,
                "current_purpose": item.purpose,
                "after_paragraph": item.placement_after_paragraph,
                "prompt": item.prompt[:500],
            }
            for item in illustrations
        ]
        if not candidates:
            raise PublicationAssetSelectionError("没有可供 Agent 决定的已生成图片")
        selected_model = model_for(self.settings, "illustration_planner")
        selected_api_key = api_key_for(self.settings, "illustration_planner")
        if not (self.settings.llm_enabled and selected_api_key and selected_model):
            raise PublicationAssetSelectionError("投递素材选择需要启用配图规划模型")
        prompt = (
            "你是公众号草稿投递素材选择器。仅从候选图片中决定 1 张封面及 0 到 6 张正文插图。"
            "返回 JSON：cover_asset_id、inline_asset_ids。封面必须选择一个候选 asset_id；正文图可为空且不得包含封面。"
            "依据文章主题、图片提示词和插入位置选择最匹配、最少但足够的图片；不得解释、不得创建新 ID、不得输出推理。"
            f"文章标题：{(draft.title_options_json or [''])[0]}；正文：{draft.body[:5000]}；候选："
            + json.dumps(candidates, ensure_ascii=False)
        )
        try:
            logger.info(
                "publication_asset_selection_started draft_id=%s model_category=illustration_planner model=%s candidate_count=%s",
                getattr(draft, "id", "unknown"), selected_model, len(candidates),
            )
            client = OpenAI(
                api_key=selected_api_key,
                base_url=base_url_for(self.settings, "illustration_planner") or None,
                max_retries=self.settings.content_llm_max_retries,
                timeout=self.settings.content_llm_timeout_seconds,
            )
            client = wrap_openai(client) if langsmith_enabled(self.settings) else client
            response = client.chat.completions.create(
                model=selected_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, temperature=0,
            )
            payload = json.loads(response.choices[0].message.content or "{}")
            allowed_ids = {item["asset_id"] for item in candidates}
            cover_asset_id = payload.get("cover_asset_id")
            if not isinstance(cover_asset_id, str) or cover_asset_id not in allowed_ids:
                raise ValueError("素材选择模型未返回有效封面")
            raw_inline = payload.get("inline_asset_ids", [])
            if not isinstance(raw_inline, list):
                raise ValueError("素材选择模型未返回正文插图数组")
            inline_asset_ids: list[str] = []
            for asset_id in raw_inline:
                if (
                    isinstance(asset_id, str)
                    and asset_id in allowed_ids
                    and asset_id != cover_asset_id
                    and asset_id not in inline_asset_ids
                ):
                    inline_asset_ids.append(asset_id)
                if len(inline_asset_ids) == 6:
                    break
            logger.info(
                "publication_asset_selection_completed draft_id=%s inline_count=%s",
                getattr(draft, "id", "unknown"), len(inline_asset_ids),
            )
            return PublicationAssetSelection(cover_asset_id, inline_asset_ids, "llm")
        except Exception as exc:
            logger.warning(
                "publication_asset_selection_failed draft_id=%s error_type=%s",
                getattr(draft, "id", "unknown"), type(exc).__name__,
            )
            raise PublicationAssetSelectionError("Agent 未能确定公众号投递素材") from exc


class AutoIllustrationTool:
    """按规划生成正文插图；生成失败不会修改原文或阻断后续人工审核。"""

    def __init__(self, settings: Settings, repository: ContentRepository):
        self.settings = settings
        self.repository = repository

    async def invoke(self, draft_id: str) -> dict:
        draft = self.repository.get_draft(draft_id)
        plan = IllustrationPlanner(self.settings).decide(draft)
        paragraphs = _paragraphs(draft.body)
        generated: list[GeneratedIllustration] = []
        for index, position in enumerate(plan.placements):
            subject = plan.subjects[index] if index < len(plan.subjects) else ""
            style = plan.styles[index] if index < len(plan.styles) else ""
            generated.append(
                await ImageGenerationTool(self.settings, self.repository).invoke(
                    draft_id, "inline", position, paragraphs[position - 1],
                    visual_direction_for("inline", position), subject, style,
                )
            )
        return {
            "tool": "auto_illustration_planner",
            "draft_id": draft_id,
            "mode": plan.mode,
            "placements": plan.placements,
            "generated_illustration_ids": [item.illustration_id for item in generated],
        }
