from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.tools.image_generation as image_generation


def test_decode_nonempty_base64_image_rejects_empty_or_invalid_data() -> None:
    assert image_generation.decode_nonempty_base64_image("") is None
    assert image_generation.decode_nonempty_base64_image("not-base64") is None
    assert image_generation.decode_nonempty_base64_image("aGVsbG8=") == b"hello"


def test_safe_image_prompt_explicitly_forbids_any_text_or_layout() -> None:
    prompt = image_generation.build_safe_prompt(
        "AI 智能体", "用于自动化任务", "cover", visual_direction="an isometric system architecture"
    )

    assert "ABSOLUTELY NO TEXT IN ANY LANGUAGE" in prompt
    assert "no Chinese characters" in prompt
    assert "not a poster or an infographic" in prompt
    assert "Distinct visual direction" in prompt


@pytest.mark.asyncio
async def test_image_generation_retries_when_ocr_detects_visible_text(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = image_generation.ImageGenerationTool.__new__(image_generation.ImageGenerationTool)
    generated = [(b"first", "image/png"), (b"second", "image/png")]

    async def fake_generate(_prompt: str):
        return generated.pop(0)

    detections = [["中文标题"], []]
    monkeypatch.setattr(tool, "_generate", fake_generate)
    monkeypatch.setattr(image_generation, "detect_visible_text", lambda _content: detections.pop(0))

    content, content_type = await tool._generate_without_visible_text("safe prompt")

    assert content == b"second"
    assert content_type == "image/png"


@pytest.mark.asyncio
async def test_image_generation_rejects_image_when_all_ocr_checks_find_text(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = image_generation.ImageGenerationTool.__new__(image_generation.ImageGenerationTool)

    async def fake_generate(_prompt: str):
        return b"image", "image/png"

    monkeypatch.setattr(tool, "_generate", fake_generate)
    monkeypatch.setattr(image_generation, "detect_visible_text", lambda _content: ["text"])

    with pytest.raises(image_generation.ImageGenerationError, match="可见文字"):
        await tool._generate_without_visible_text("safe prompt")


@pytest.mark.asyncio
async def test_image_generation_falls_back_to_https_url_when_b64_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict | None = None, content: bytes = b"", content_type: str = "image/png") -> None:
            self.payload = payload or {}
            self.content = content
            self.headers = {"content-type": content_type}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class FakeClient:
        requested_url = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **kwargs):
            assert kwargs["json"]["extra_body"] == {"response_format": "b64_json"}
            assert kwargs["json"]["size"] == "1K"
            assert kwargs["json"]["ratio"] == "4:3"
            return FakeResponse({"data": [{"b64_json": "", "url": "https://images.example.test/test.png"}]})

        async def get(self, url: str):
            self.requested_url = url
            return FakeResponse(content=b"png-bytes")

    client = FakeClient()
    monkeypatch.setattr(image_generation.httpx, "AsyncClient", lambda **_kwargs: client)
    tool = image_generation.ImageGenerationTool.__new__(image_generation.ImageGenerationTool)
    tool.settings = SimpleNamespace(
        image_generation_base_url="https://api.example.test",
        image_generation_api_key="test-key",
        image_generation_model="test-model",
        image_generation_size="1K",
        image_generation_ratio="4:3",
        image_generation_timeout_seconds=1,
    )

    content, content_type = await tool._generate("test prompt")

    assert content == b"png-bytes"
    assert content_type == "image/png"
    assert client.requested_url == "https://images.example.test/test.png"
