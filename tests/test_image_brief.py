"""四个来源的 image-brief：风格/实物池解析、池内取用、哈希轮换与缺失兜底。"""

from __future__ import annotations

import pytest

from app.domain.models import SourceKind
from app.services import image_brief
from app.services.image_brief import (
    FALLBACK_SCENE,
    SOURCE_IMAGE_BRIEF_FOLDERS,
    load_brief,
    object_at,
    object_index,
    pick_object,
    pick_style,
    style_at,
)
from app.tools.image_generation import build_safe_prompt
from app.tools.illustration_planner import INLINE_VISUAL_DIRECTIONS, visual_direction_for


@pytest.mark.parametrize("source_kind", list(SOURCE_IMAGE_BRIEF_FOLDERS))
def test_every_source_brief_has_a_style_pool_and_an_object_pool(source_kind: SourceKind) -> None:
    brief = load_brief(source_kind)

    assert brief is not None
    # 两套池子都要够大：风格 12 条、实物 22 个以上，组合数因此达到数百种。
    assert len(brief.styles) >= 12
    assert len(brief.objects) >= 20
    assert len(set(brief.styles)) == len(brief.styles)
    assert len(set(brief.objects)) == len(brief.objects)
    # 风格句里必须真的带有介质、光线/视角、镜头或印刷方式与色调，而不是一句笼统的风格形容词。
    for style in brief.styles:
        lowered = style.lower()
        assert "photograph" in lowered or "print" in lowered
        assert "palette:" in lowered
        assert "f/" in style or "view" in lowered


@pytest.mark.parametrize("source_kind", list(SOURCE_IMAGE_BRIEF_FOLDERS))
def test_pool_items_are_source_specific(source_kind: SourceKind) -> None:
    brief = load_brief(source_kind)
    assert brief is not None
    other = [kind for kind in SOURCE_IMAGE_BRIEF_FOLDERS if kind != source_kind][0]
    other_brief = load_brief(other)
    assert other_brief is not None

    assert not set(brief.objects) & set(other_brief.objects)
    assert not set(brief.styles) & set(other_brief.styles)


def test_style_and_object_rotate_independently_within_a_draft() -> None:
    brief = load_brief(SourceKind.GITHUB)
    assert brief is not None

    first = pick_style(brief, "draft-a", "inline"), pick_object(brief, "draft-a", "inline", 1)
    second = pick_style(brief, "draft-a", "inline"), pick_object(brief, "draft-a", "inline", 2)
    # 风格是文章级的（同一篇不变），实物按位置变化。
    assert first[0] == second[0]
    assert first[1] != second[1]
    # 换一篇则风格起点也不同（30 篇至少命中 4 条不同风格）。
    assert len({pick_style(brief, f"draft-{index}", "cover") for index in range(30)}) >= 4
    assert style_at(brief, len(brief.styles)) == brief.styles[0]


def test_object_index_is_stable_per_draft_and_varies_across_drafts() -> None:
    count = 12
    same = {object_index("draft-a", "cover", 0, count) for _ in range(5)}
    assert len(same) == 1
    spread = {object_index(f"draft-{index}", "cover", 0, count) for index in range(40)}
    # 40 篇不同文章应当触及池内多个不同实物，而不是永远同一个。
    assert len(spread) >= 4
    # 同一篇内位置顺延：1/2/3 三个位置落在池内相邻且互不相同的条目。
    indexes = [object_index("draft-a", "inline", position, count) for position in (1, 2, 3)]
    assert len(set(indexes)) == 3
    assert indexes[1] == (indexes[0] + 1) % count
    # 没有草稿 ID 时退回按位置轮换。
    assert object_index(None, "inline", 1, count) == 0
    assert object_index(None, "inline", 2, count) == 1


def test_object_at_wraps_around_the_pool() -> None:
    brief = load_brief(SourceKind.GITHUB)
    assert brief is not None

    assert object_at(brief, 0) == brief.objects[0]
    assert object_at(brief, len(brief.objects)) == brief.objects[0]
    assert pick_object(brief, "draft-x", "cover", 0) in brief.objects
    assert pick_object(None, "draft-x", "cover", 0) == ""


def test_unknown_source_or_missing_brief_falls_back_to_the_neutral_scene(monkeypatch) -> None:
    assert load_brief(SourceKind.ATTACHMENT) is None
    assert pick_object(None, "draft-x", "cover", 0) == ""
    monkeypatch.setattr(image_brief, "resolve_asset", lambda _relative: None)
    assert load_brief(SourceKind.GITHUB) is None

    prompt = build_safe_prompt("标题", "摘要", "cover", source_kind=SourceKind.ATTACHMENT)

    assert FALLBACK_SCENE in prompt
    assert "No Chinese characters or other CJK glyphs" in prompt


def test_unreadable_brief_degrades_without_raising(monkeypatch) -> None:
    class BrokenPath:
        def read_text(self, **_kwargs):
            raise OSError("boom")

    monkeypatch.setattr(image_brief, "resolve_asset", lambda _relative: BrokenPath())

    assert load_brief(SourceKind.RSS) is None


def test_incomplete_brief_is_rejected(monkeypatch) -> None:
    class HalfPath:
        text = "# 标题\n\n## 风格池\n\n1. A calm editorial photograph on paper, 50mm, f/4. Palette: grey.\n"

        def read_text(self, **_kwargs):
            return self.text

    monkeypatch.setattr(image_brief, "resolve_asset", lambda _relative: HalfPath())

    # 只有风格池、没有实物池的 brief 不可用。
    assert load_brief(SourceKind.RSS) is None


def test_styles_differ_between_sources() -> None:
    styles = {kind: load_brief(kind).style for kind in SOURCE_IMAGE_BRIEF_FOLDERS}

    assert len(set(styles.values())) == len(styles)


def test_fallback_visual_directions_do_not_reintroduce_the_ai_look() -> None:
    directions = [visual_direction_for("cover", 1), *INLINE_VISUAL_DIRECTIONS]

    assert len(set(directions)) == len(directions)
    # brief 缺失时的兜底也必须是真实介质，且不得出现旧的 AI 味构图词。
    assert all(any(word in item for word in ("photograph", "risograph", "print")) for item in directions)
    forbidden = ("3d render", "isometric", "abstract data", "holographic", "neon", "circuit", "hero composition")
    assert [word for word in forbidden if any(word in item.lower() for item in directions)] == []
