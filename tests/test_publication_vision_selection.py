"""视觉选图：把真实截图给模型看，先识别内容再决定封面。

实测：配置的 `deepseek-flash`（同端点）能正确识别图像内容（纯蓝方块 → “蓝色”），
但它偶尔只输出推理不输出正文，所以必须保留回退路径。
"""

from __future__ import annotations

import base64
import inspect

from app.tools.illustration_planner import IllustrationPlanner, _selection_message


class _Item:
    def __init__(self, asset_id: str, provider, prompt: str = "", purpose: str = "inline", placement: int = 1) -> None:
        self.asset_id = asset_id
        self.provider = provider
        self.prompt = prompt
        self.model = None
        self.purpose = purpose
        self.placement_after_paragraph = placement


def test_only_source_images_are_loaded_for_vision() -> None:
    planner = IllustrationPlanner(type("S", (), {"publication_vision_selection_enabled": True})())
    candidates = [
        {"asset_id": "shot-1", "origin": "source"},
        {"asset_id": "ai-1", "origin": "generated"},
    ]
    loaded: list[str] = []

    def loader(asset_id: str) -> bytes | None:
        loaded.append(asset_id)
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    images = planner._load_source_images(candidates, loader)

    assert loaded == ["shot-1"]          # AI 配图不读图，省 token
    assert set(images) == {"shot-1"}
    assert images["shot-1"].startswith("data:image/png;base64,")
    assert base64.b64decode(images["shot-1"].split(",", 1)[1])


def test_loader_failure_is_tolerated() -> None:
    planner = IllustrationPlanner(type("S", (), {"publication_vision_selection_enabled": True})())

    def broken(_asset_id: str) -> bytes | None:
        raise RuntimeError("minio down")

    assert planner._load_source_images([{"asset_id": "s", "origin": "source"}], broken) == {}


def test_message_is_text_only_without_images() -> None:
    assert _selection_message("提示词", {}) == "提示词"

    content = _selection_message("提示词", {"shot-1": "data:image/png;base64,AAAA"})
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert any(part.get("type") == "image_url" for part in content)
    assert any("asset_id=shot-1" in part.get("text", "") for part in content)


def test_prompt_asks_to_recognise_images_then_pick_cover() -> None:
    source = inspect.getsource(IllustrationPlanner.decide_publication_assets)

    assert "先识别每张官方图的实际画面内容" in source
    assert "优先官方截图里最能说明“这是什么”的一张" in source
    assert "max_tokens=1500" in source  # 视觉/推理模型预算，避免空回复
