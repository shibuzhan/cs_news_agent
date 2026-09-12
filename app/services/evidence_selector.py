"""证据取材：按标题分节，交给模型挑选要调取的章节；失败时回退到确定性挑选。

分工：`split_markdown_sections()` 负责结构，模型只回答"要哪几节"（返回编号，不返回内容），
服务端按编号取原文；模型不可用、返回非法编号或选得太少时，回退到关键词挑选，
因此取材环节永远不会因为模型问题而中断生成。
"""

from __future__ import annotations

import json
import logging

from langsmith.wrappers import wrap_openai
from openai import OpenAI

from app.config import Settings, api_key_for, base_url_for, model_for
from app.observability import langsmith_enabled
from app.services.plain_text import MarkdownSection, select_evidence_text, split_markdown_sections


logger = logging.getLogger("news_agent.evidence_selector")

# 章节清单送进提示词时的预览长度；章节很多时收紧，避免提示词过长。
_PREVIEW_CHARS = 120
_PREVIEW_CHARS_DENSE = 60
_DENSE_SECTION_COUNT = 40
# 模型选出的内容少于预算的这个比例时，用关键词挑选补齐。
_MIN_FILL_RATIO = 0.4


def _preview(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def build_section_prompt(sections: list[MarkdownSection], budget: int) -> str:
    """把章节清单（编号、标题、字数、开头预览）交给模型，只要求返回编号。"""
    limit = _PREVIEW_CHARS if len(sections) <= _DENSE_SECTION_COUNT else _PREVIEW_CHARS_DENSE
    lines = [
        f"{index}. {section.title or '（开头，无标题）'} | {len(section.body)} 字 | {_preview(section.body, limit)}"
        for index, section in enumerate(sections)
    ]
    return (
        "你是科技资讯取材编辑。下面是来源文档的章节清单（编号、标题、字数、开头预览）。"
        "请挑选**与“项目背景、它能做什么、怎么用”最相关**的章节，按重要性排序；"
        "跳过安装步骤、价格与套餐、授权许可、贡献指南、更新日志、语言清单、徽章与渠道声明。"
        f"所选章节原文总字数不要超过 {budget} 字，宁少勿滥；开头概览章节优先保留。"
        '只返回 JSON：{"sections":[编号, 编号]}，不要输出解释或其它字段。\n'
        + "\n".join(lines)
    )


def parse_section_numbers(payload: str, count: int) -> list[int]:
    """解析模型返回的章节编号：去非法值、去重、保持模型给出的顺序。"""
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return []
    raw = data.get("sections") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    chosen: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if 0 <= item < count and item not in chosen:
            chosen.append(item)
    return chosen


def _assemble(sections: list[MarkdownSection], chosen: list[int], budget: int) -> str:
    """按编号取原文并拼装；单节超出剩余预算时在该节内部再做一次块挑选。"""
    parts: list[str] = []
    used = 0
    for index in chosen:
        section = sections[index]
        text = section.text
        if used + len(text) + 2 > budget:
            remaining = budget - used - 2
            if remaining < 200:
                continue
            text = select_evidence_text(section.body, remaining)
            if not text:
                continue
        parts.append(text)
        used += len(text) + 2
    return "\n\n".join(parts)[:budget]


def choose_sections(
    settings: Settings, sections: list[MarkdownSection], budget: int, source_ref: str = ""
) -> list[int]:
    """让模型挑章节；任何失败都返回空列表，由调用方回退到关键词挑选。"""
    selected_model = model_for(settings, "evidence_selector")
    selected_api_key = api_key_for(settings, "evidence_selector")
    if not (getattr(settings, "llm_enabled", False) and selected_api_key and selected_model):
        return []
    try:
        logger.info(
            "evidence_selection_started source_ref=%s model=%s sections=%s budget=%s",
            source_ref,
            selected_model,
            len(sections),
            budget,
        )
        client = OpenAI(
            api_key=selected_api_key,
            base_url=base_url_for(settings, "evidence_selector") or None,
            max_retries=getattr(settings, "content_llm_max_retries", 0),
            timeout=getattr(settings, "content_llm_timeout_seconds", 600),
        )
        client = wrap_openai(client) if langsmith_enabled(settings) else client
        response = client.chat.completions.create(
            model=selected_model,
            messages=[{"role": "user", "content": build_section_prompt(sections, budget)}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        chosen = parse_section_numbers(response.choices[0].message.content or "", len(sections))
        logger.info("evidence_selection_finished source_ref=%s sections=%s", source_ref, chosen)
        return chosen
    except Exception as exc:  # 取材失败不能中断生成
        logger.warning("evidence_selection_failed source_ref=%s error_type=%s", source_ref, type(exc).__name__)
        return []


def build_evidence(settings: Settings, content: str, budget: int, source_ref: str = "") -> str:
    """生成/改稿使用的来源证据文本。"""
    text = (content or "").strip()
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    sections = split_markdown_sections(text)
    if len(sections) < 2:
        # 没有标题结构（例如历史快照或纯文本来源）：回退到确定性块挑选。
        return select_evidence_text(text, budget)
    chosen = choose_sections(settings, sections, budget, source_ref)
    if not chosen:
        return select_evidence_text(text, budget)
    assembled = _assemble(sections, chosen, budget)
    if len(assembled) < budget * _MIN_FILL_RATIO:
        extra = select_evidence_text(text, budget - len(assembled) - 2)
        if extra:
            assembled = f"{assembled}\n\n{extra}"[:budget]
    logger.info(
        "evidence_built source_ref=%s mode=model sections=%s chars=%s",
        source_ref,
        chosen,
        len(assembled),
    )
    return assembled
