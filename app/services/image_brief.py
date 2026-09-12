"""按来源读取项目内 image-brief Skill：提供可替换的风格池与候选实物池。

职责划分：Skill 决定**风格与可选实物**（静态，随来源不同）；服务端决定**取哪一条风格、哪一个实物、
以及构图与约束**（动态）。风格按文章取（同一篇共用），实物按每张图取；两者都只能取池内条目——
正文插图由规划模型按文章内容选（只返回编号），失败或越界时按草稿 ID 哈希轮换。同一来源的不同文章
因此不会重复同一张图。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from app.domain.models import SourceKind
from app.paths import resolve_asset


logger = logging.getLogger("news_agent.image_brief")

SOURCE_IMAGE_BRIEF_FOLDERS: dict[SourceKind, str] = {
    SourceKind.ARXIV: "arxiv-content-writing",
    SourceKind.GITHUB: "github-content-writing",
    SourceKind.HACKER_NEWS: "hacker-news-content-writing",
    SourceKind.RSS: "rss-content-writing",
}
STYLE_HEADING = "风格"
STYLE_POOL_HEADING = "风格池"
OBJECTS_HEADING = "实物池"
# 池内条目的行首标记：`1. `、`- `、`* `、`• ` 等。只有带标记的行才算池内条目，
# 小节里的说明文字因此不会被当成实物或风格。
_ITEM_MARKER = re.compile(r"^\s*(?:[-*•]|\d+\s*[.)、])\s+")
_ITEM_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+\s*[.)、])\s*")
# 实物条目的类别标记：`[desk] a mechanical keyboard …`；用于同一篇内避开同一类实物。
_CATEGORY_PREFIX = re.compile(r"^\[([a-z0-9_-]+)\]\s*")
# 来源 brief 缺失（未知来源或资源目录不可用）时的中性兜底场景。
FALLBACK_SCENE = (
    "Editorial photograph of a calm neutral desk surface with one simple everyday object, "
    "soft directional daylight, matte materials, shallow depth of field, nothing written anywhere, "
    "50mm, f/2.8, fine film grain"
)


@dataclass(frozen=True)
class ImageBrief:
    """一个来源的配图规则：可替换的风格池 + 候选实物池。"""

    styles: tuple[str, ...]
    objects: tuple[str, ...]
    # 与 objects 等长；空串表示该条目没有类别标记。
    categories: tuple[str, ...] = ()

    @property
    def style(self) -> str:
        """第一条风格，便于查看与兜底。"""
        return self.styles[0] if self.styles else ""


def _parse_blocks(text: str) -> dict[str, list[str]]:
    """按 `## 标题` 切块，保留块内每一行（去空行）；标题之外的散落说明忽略。"""
    blocks: dict[str, list[str]] = {}
    heading: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            heading = line[3:].strip()
            blocks.setdefault(heading, [])
            continue
        if heading is not None and line.strip():
            blocks[heading].append(line.strip())
    return blocks


def _pool_items(lines: list[str]) -> tuple[str, ...]:
    """把一节里带编号或项目符号的行转成池内条目；小节内的说明文字直接忽略。"""
    return tuple(
        cleaned
        for cleaned in (
            _ITEM_PREFIX.sub("", line).strip() for line in lines if _ITEM_MARKER.match(line)
        )
        if cleaned
    )


def _object_items(lines: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """解析实物条目：`[类别] 描述`，返回（描述, 类别）两个等长元组。"""
    objects: list[str] = []
    categories: list[str] = []
    for cleaned in _pool_items(lines):
        match = _CATEGORY_PREFIX.match(cleaned)
        if match:
            categories.append(match.group(1))
            objects.append(cleaned[match.end():].strip())
        else:
            categories.append("")
            objects.append(cleaned)
    return tuple(objects), tuple(categories)


def load_brief(source_kind: SourceKind | str | None) -> ImageBrief | None:
    """读取该来源的 brief；来源未知、文件缺失、风格或实物池为空时返回 None。"""
    try:
        kind = SourceKind(source_kind) if source_kind is not None else None
    except ValueError:
        kind = None
    folder = SOURCE_IMAGE_BRIEF_FOLDERS.get(kind) if kind is not None else None
    if folder is None:
        return None
    path = resolve_asset(f"{folder}/references/image-brief.md")
    if path is None:
        logger.warning("image_brief_unavailable source=%s folder=%s", kind.value, folder)
        return None
    try:
        blocks = _parse_blocks(path.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("image_brief_unreadable source=%s folder=%s", kind.value, folder)
        return None
    style_lines = blocks.get(STYLE_POOL_HEADING) or blocks.get(STYLE_HEADING) or []
    styles = _pool_items(style_lines)
    objects, categories = _object_items(blocks.get(OBJECTS_HEADING, []))
    if not styles or not objects:
        logger.warning(
            "image_brief_incomplete source=%s folder=%s styles=%s objects=%s",
            kind.value, folder, len(styles), len(objects),
        )
        return None
    return ImageBrief(styles=styles, objects=objects, categories=categories)


def object_index(
    draft_id: str | None,
    purpose: str,
    placement_after_paragraph: int,
    count: int,
    *,
    salt: str = "",
) -> int:
    """池内编号：起点由草稿 ID 决定，再按插图位置顺延。

    同一篇的起点固定（重跑得到同一张图），一篇内不同位置又顺延到不同实物；
    不同文章的起点不同，因此同一来源的不同文章不会重复同一张图。
    `salt` 用于让风格池与实物池在同一篇里各自独立取值。
    """
    if count <= 0:
        return 0
    offset = 0
    if draft_id:
        seed = f"{draft_id}:{purpose}{salt}"
        offset = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:4], "big")
    return (offset + max(placement_after_paragraph, 1) - 1) % count


def object_at(brief: ImageBrief, index: int) -> str:
    """池内取实物；编号越界时按池长度回绕。"""
    if not brief.objects:
        return ""
    return brief.objects[index % len(brief.objects)]


def style_at(brief: ImageBrief, index: int) -> str:
    """池内取风格；编号越界时按池长度回绕。"""
    if not brief.styles:
        return ""
    return brief.styles[index % len(brief.styles)]


def pick_object(brief: ImageBrief | None, draft_id: str | None, purpose: str, placement_after_paragraph: int) -> str:
    """哈希轮换地挑一个实物；没有 brief 时返回空串（由调用方退回视觉方向）。"""
    if brief is None or not brief.objects:
        return ""
    return object_at(
        brief, object_index(draft_id, purpose, placement_after_paragraph, len(brief.objects))
    )


def pick_style(
    brief: ImageBrief | None,
    draft_id: str | None,
    purpose: str,
    placement_after_paragraph: int = 1,
) -> str:
    """哈希轮换地挑一条风格；同一篇内按插图位置继续错开，因此每张图的风格也不同。"""
    if brief is None or not brief.styles:
        return ""
    return style_at(
        brief,
        object_index(
            draft_id, purpose, placement_after_paragraph, len(brief.styles), salt=":style"
        ),
    )


def category_at(brief: ImageBrief, index: int) -> str:
    if not brief.categories:
        return ""
    return brief.categories[index % len(brief.categories)]


def spread_categories(
    brief: ImageBrief | None,
    chosen: tuple[str, ...],
    draft_id: str | None,
    purpose: str,
    positions: list[int],
) -> tuple[str, ...]:
    """让模型选中的实物也遵守“同篇不撞类别”：撞类别的那张改用池内其他类别的实物。"""
    if brief is None or not brief.objects or not chosen:
        return chosen
    count = len(brief.objects)
    used: set[str] = set()
    result: list[str] = []
    for index, subject in enumerate(chosen):
        position = positions[index] if index < len(positions) else 1
        try:
            item_index = brief.objects.index(subject)
        except ValueError:
            item_index = object_index(draft_id, purpose, position, count)
        category = category_at(brief, item_index)
        if not category or category not in used:
            if category:
                used.add(category)
            result.append(subject)
            continue
        replacement = item_index
        for offset in range(1, count + 1):
            candidate = (item_index + offset) % count
            candidate_category = category_at(brief, candidate)
            if candidate_category and candidate_category not in used:
                replacement = candidate
                break
        else:
            replacement = item_index
        replacement_category = category_at(brief, replacement)
        if replacement_category:
            used.add(replacement_category)
        result.append(brief.objects[replacement])
    return tuple(result)


def pick_objects(
    brief: ImageBrief | None,
    draft_id: str | None,
    purpose: str,
    positions: list[int],
) -> tuple[str, ...]:
    """为同一篇的多个位置挑实物，并尽量避开同一类别（不要三张都是桌面小件）。"""
    if brief is None or not brief.objects:
        return tuple("" for _ in positions)
    count = len(brief.objects)
    used_categories: set[str] = set()
    picked: list[str] = []
    for position in positions:
        start = object_index(draft_id, purpose, position, count)
        chosen = start
        for offset in range(count):
            candidate = (start + offset) % count
            category = category_at(brief, candidate)
            if not category or category not in used_categories:
                chosen = candidate
                break
        category = category_at(brief, chosen)
        if category:
            used_categories.add(category)
        picked.append(brief.objects[chosen])
    return tuple(picked)
