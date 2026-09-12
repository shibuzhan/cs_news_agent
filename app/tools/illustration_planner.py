"""受控配图规划：决定数量与位置，不暴露模型推理过程。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from langsmith.wrappers import wrap_openai
from openai import OpenAI

from app.config import Settings, api_key_for, base_url_for, model_for
from app.observability import langsmith_enabled
from app.storage.repositories import ContentRepository
from app.tools.image_generation import GeneratedIllustration, ImageGenerationTool


logger = logging.getLogger("news_agent.illustration_planner")
MAX_AUTO_ILLUSTRATIONS = 3

# 同一篇文章的插图通过不同的视觉角色区分，避免每一段都生成同一种蓝色芯片或代码屏幕。
INLINE_VISUAL_DIRECTIONS = (
    "an isometric system architecture with layered modules and flowing connections",
    "a calm developer workflow scene using abstract tools and physical desk objects, no people",
    "an abstract data-flow landscape with distinct input, processing, and output structures",
    "a close-up conceptual mechanism showing constraints, checks, and feedback loops",
    "a wide technical environment illustrating deployment or real-world use context",
)


def visual_direction_for(purpose: str, placement_after_paragraph: int) -> str:
    """稳定地给异步图片任务分配不同构图，不依赖队列中的临时内存。"""
    if purpose == "cover":
        return "a high-information-density editorial hero composition with one clear focal technical concept"
    return INLINE_VISUAL_DIRECTIONS[(max(placement_after_paragraph, 1) - 1) % len(INLINE_VISUAL_DIRECTIONS)]


@dataclass(frozen=True)
class IllustrationPlan:
    placements: list[int]
    mode: str


@dataclass(frozen=True)
class PublicationAssetSelection:
    cover_asset_id: str
    inline_asset_ids: list[str]
    mode: str


class PublicationAssetSelectionError(RuntimeError):
    """投递素材必须由模型确定时的安全失败。"""


def _paragraphs(body: str) -> list[str]:
    return [
        item.strip() for item in body.replace("\r", "").split("\n\n")
        if item.strip() and not item.lstrip("　 ").startswith("原文标题：")
    ]


def fallback_plan(body: str) -> IllustrationPlan:
    paragraphs = _paragraphs(body)
    if len(paragraphs) < 3:
        return IllustrationPlan([], "deterministic")
    if len(paragraphs) >= 6:
        return IllustrationPlan([2, 4, 5], "deterministic")
    if len(paragraphs) >= 5:
        return IllustrationPlan([2, 4], "deterministic")
    return IllustrationPlan([2], "deterministic")


class IllustrationPlanner:
    def __init__(self, settings: Settings):
        self.settings = settings

    def decide(self, draft) -> IllustrationPlan:
        paragraphs = _paragraphs(draft.body)
        selected_model = model_for(self.settings, "illustration_planner")
        selected_api_key = api_key_for(self.settings, "illustration_planner")
        if not (self.settings.llm_enabled and selected_api_key and selected_model):
            return fallback_plan(draft.body)
        prompt = (
            "你是资讯文章配图规划器。只返回 JSON：placements（整数数组）。"
            "根据正文结构决定是否需要正文插图、插几张及每张应插在第几段后。"
            f"最多 {MAX_AUTO_ILLUSTRATIONS} 张；没有必要可返回空数组。"
            "禁止封面、品牌、人物、截图或正文以外的信息。不要输出解释或推理。"
            f"文章标题：{(draft.title_options_json or [''])[0]}；正文段落："
            + json.dumps(paragraphs, ensure_ascii=False)
        )
        try:
            logger.info(
                "illustration_plan_llm_started draft_id=%s model_category=illustration_planner model=%s",
                getattr(draft, "id", "unknown"),
                selected_model,
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
            raw = payload.get("placements", [])
            placements = sorted({int(item) for item in raw if isinstance(item, int) and 1 <= item <= len(paragraphs)})[:MAX_AUTO_ILLUSTRATIONS]
            return IllustrationPlan(placements, "llm")
        except Exception as exc:
            logger.warning("illustration_plan_llm_fallback error_type=%s", type(exc).__name__)
            return fallback_plan(draft.body)

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
        for position in plan.placements:
            generated.append(
                await ImageGenerationTool(self.settings, self.repository).invoke(
                    draft_id, "inline", position, paragraphs[position - 1],
                    visual_direction_for("inline", position),
                )
            )
        return {
            "tool": "auto_illustration_planner",
            "draft_id": draft_id,
            "mode": plan.mode,
            "placements": plan.placements,
            "generated_illustration_ids": [item.illustration_id for item in generated],
        }
