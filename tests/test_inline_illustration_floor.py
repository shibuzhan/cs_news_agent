"""正文插图保底与“真实截图优先”口径。

真实反馈：把封面换成官方截图后，模型把 3 张氛围类 AI 配图**整批排除**，正文变成 0 张插图。
口径应为“真实截图优先，缺口用 AI 配图补足”，因此除提示词外再加确定性保底。
"""

from __future__ import annotations

import inspect

from app.tools.illustration_planner import MIN_INLINE_ILLUSTRATIONS, _apply_selection_policy, _top_up_inline


def test_policy_forces_source_screenshot_into_inline() -> None:
    """模型只挑 AI 图时，真实截图仍必须进正文（“真实图优先”不能只靠提示词）。"""
    candidates = [
        _candidate("cover", "source", 0),
        _candidate("shot-2", "source", 3),
        _candidate("ai-1", "generated", 1),
        _candidate("ai-2", "generated", 2),
        _candidate("ai-3", "generated", 5),
    ]

    picked = _apply_selection_policy(["ai-1", "ai-2", "ai-3"], candidates, "cover", minimum=2)

    assert picked[0] == "shot-2"          # 真实截图排在最前
    assert set(picked) >= {"shot-2", "ai-1", "ai-2", "ai-3"}
    assert "cover" not in picked


def test_policy_keeps_model_picks_and_tops_up_generated() -> None:
    candidates = [_candidate("cover", "source", 0), _candidate("ai-1", "generated", 1), _candidate("ai-2", "generated", 2)]

    assert _apply_selection_policy([], candidates, "cover", minimum=2) == ["ai-1", "ai-2"]
    assert _apply_selection_policy(["ai-2"], candidates, "cover", minimum=1) == ["ai-2"]


def test_policy_respects_maximum() -> None:
    candidates = [_candidate("cover", "source", 0)] + [_candidate(f"ai-{i}", "generated", i) for i in range(1, 10)]
    model_picks = [f"ai-{i}" for i in range(1, 10)]

    picked = _apply_selection_policy(model_picks, candidates, "cover", minimum=2, maximum=6)
    assert len(picked) == 6
    # 只保底、不强行填满：候选不足或模型只选少量 AI 时按 minimum 执行。
    assert len(_apply_selection_policy([], candidates, "cover", minimum=2)) == 2



def _candidate(asset_id: str, origin: str, paragraph: int) -> dict:
    return {"asset_id": asset_id, "origin": origin, "after_paragraph": paragraph}


def test_floor_keeps_model_choices_and_tops_up_with_sources_first() -> None:
    candidates = [
        _candidate("cover", "source", 0),
        _candidate("ai-1", "generated", 2),
        _candidate("ai-2", "generated", 3),
        _candidate("shot-2", "source", 5),
    ]

    # 模型一张都没选：补到 2 张，且真实截图排在前面。
    picked = _top_up_inline([], candidates, "cover", minimum=MIN_INLINE_ILLUSTRATIONS)
    assert picked == ["shot-2", "ai-1"]

    # 模型已选够时不动它的选择。
    assert _top_up_inline(["ai-2"], candidates, "cover", minimum=1) == ["ai-2"]

    # 封面永远不会被当作正文插图。
    assert "cover" not in _top_up_inline([], candidates, "cover", minimum=4)


def test_floor_never_exceeds_available_candidates() -> None:
    candidates = [_candidate("cover", "source", 0), _candidate("ai-1", "generated", 2)]

    assert _top_up_inline([], candidates, "cover", minimum=5) == ["ai-1"]


def test_planner_prompt_prefers_source_screenshots() -> None:
    from app.tools import illustration_planner

    source = inspect.getsource(illustration_planner.IllustrationPlanner.decide_publication_assets)

    assert "真实截图（origin=source）优先" in source
    assert "不得因为“与文章主题不完全对应”就整批排除" in source
    assert "_apply_selection_policy(" in source


def test_source_detection_uses_provider_field() -> None:
    """真实截图靠 `provider` 判据识别：AI 图有供应商，真实素材没有。"""
    from app.tools.illustration_planner import _is_source_asset

    real = type("Row", (), {"provider": None, "model": None, "prompt": "", "asset_name": ""})()
    generated = type("Row", (), {"provider": "agnes", "model": "agnes-image-2.5-flash", "prompt": "Wide photo"})()
    prefixed = type("Row", (), {"provider": None, "model": None, "prompt": "", "asset_name": "source-shot.png"})()

    assert _is_source_asset(real) is True
    assert _is_source_asset(prefixed) is True
    assert _is_source_asset(generated) is False


def test_source_images_are_marked_with_prefix() -> None:
    from app.agent_tools import source_media_tools

    assert 'original_name=f"source-{downloaded.filename}"' in inspect.getsource(source_media_tools)
