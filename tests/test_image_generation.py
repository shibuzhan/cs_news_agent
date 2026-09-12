from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.tools.image_generation as image_generation
from app.domain.models import SourceKind
from app.services import image_text_guard
from app.services.image_brief import load_brief


def test_publication_asset_prompt_defaults_to_keeping_relevant_illustrations() -> None:
    """投递素材选择的口径是“默认保留相关图”，不是“最少但足够”。

    历史上提示词写着“选择最匹配、最少但足够的图片”，模型于是 3 张正文候选只留 1 张。
    """
    import inspect

    from app.tools import illustration_planner

    source = inspect.getsource(illustration_planner.IllustrationPlanner.decide_publication_assets)

    assert "最少但足够" not in source
    assert "与所在段落内容相关的就保留" in source
    assert "不要只保留一张" in source
    assert "正文插图通常保留 2 到 3 张" in source



def test_decode_nonempty_base64_image_rejects_empty_or_invalid_data() -> None:
    assert image_generation.decode_nonempty_base64_image("") is None
    assert image_generation.decode_nonempty_base64_image("not-base64") is None
    assert image_generation.decode_nonempty_base64_image("aGVsbG8=") == b"hello"


def test_safe_image_prompt_forbids_text_and_keeps_a_positive_allow_list() -> None:
    prompt = image_generation.build_safe_prompt(
        "AI 智能体", "用于自动化任务", "cover", visual_direction="an editorial photograph of a desk"
    )

    # 唯一保留的负向约束：中文/CJK 字形、品牌标识与人物。
    assert "No Chinese characters or other CJK glyphs" in prompt
    assert "no logos, watermarks, people or hands" in prompt
    # 字母与数字不再被禁止（尺子刻度、键帽、包装印刷属于实物细节）。
    assert "No text, letters, numbers" not in prompt
    # 构图改为正向允许清单，不再逐条列举要避免的元素。
    assert "Show only the objects described above" in prompt
    # 来源 brief 缺失时才使用调用方给出的方向。
    assert "an editorial photograph of a desk" in prompt


def test_safe_image_prompt_never_names_the_ai_cliche_motifs() -> None:
    """实测把风格类禁令写进提示词会让模型反而画出它们，因此这些名词必须不出现在提示词里。"""
    prompt = image_generation.build_safe_prompt(
        "AI 智能体与芯片", "摘要提到大脑、电路板与霓虹", "cover", source_kind="github"
    ).lower()

    for motif in (
        "holographic", "neon", "isometric", "3d render", "watercolour", "watercolor",
        "circuit", "microchip", "brain", "gradient", "glossy", "stylized", "stock photo",
    ):
        assert motif not in prompt, motif


def test_safe_image_prompt_uses_the_source_style_and_a_pool_object() -> None:
    prompt = image_generation.build_safe_prompt(
        "owner/repo：一款开发工具", "摘要说明", "cover", source_kind="github", draft_id="draft-1"
    )
    brief = load_brief(SourceKind.GITHUB)
    assert brief is not None

    # 介质、光线、材质、镜头与色调来自风格池里的一条，主体来自实物池里的一个。
    assert any(style in prompt for style in brief.styles)
    assert "Subject: " in prompt
    assert any(item in prompt for item in brief.objects)
    assert "Background context for tone only" in prompt
    assert "owner/repo：一款开发工具" in prompt
    assert "abstract data flows" not in prompt
    assert "isometric system architecture" not in prompt
    # 旧模板的空字段与双句点问题不再出现。
    assert "Nearby paragraph semantics" not in prompt
    assert "。. " not in prompt


def test_same_source_varies_across_drafts_and_stays_stable_within_one() -> None:
    def parts_for(draft_id: str, position: int = 1) -> tuple[str, str]:
        prompt = image_generation.build_safe_prompt(
            "标题", "摘要", "inline", source_kind="github", draft_id=draft_id,
            placement_after_paragraph=position,
        )
        subject = prompt.split("Subject: ", 1)[1].split(". Composition", 1)[0]
        style = prompt.split("Subject: ", 1)[0]
        return style, subject

    assert parts_for("draft-a") == parts_for("draft-a")
    # 同一篇内每张图的风格与实物都按位置错开，因此不会三张一样。
    assert len({parts_for("draft-a", position)[0] for position in (1, 2, 3)}) == 3
    assert len({parts_for("draft-a", position)[1] for position in (1, 2, 3)}) == 3
    # 不同文章在风格与实物上都应出现多种取值。
    assert len({parts_for(f"draft-{index}")[0] for index in range(30)}) >= 4
    assert len({parts_for(f"draft-{index}")[1] for index in range(30)}) >= 4


def test_explicit_style_and_subject_win_over_rotation() -> None:
    prompt = image_generation.build_safe_prompt(
        "标题", "摘要", "cover", source_kind="github",
        style="Studio photograph on a deep navy backdrop, 85mm, f/5.6. Palette: navy and brass.",
        subject="a single brass key on a matte desk",
    )

    assert prompt.startswith("Studio photograph on a deep navy backdrop")
    assert "Subject: a single brass key on a matte desk." in prompt


def test_cover_and_inline_use_different_composition_rules() -> None:
    cover = image_generation.build_safe_prompt("标题", "摘要", "cover", source_kind="arxiv", draft_id="d")
    inline = image_generation.build_safe_prompt(
        "标题", "摘要", "inline", source_kind="arxiv", draft_id="d", placement_after_paragraph=2
    )

    assert "empty space on one side for a headline" in cover
    assert "close, intimate view" in inline


@pytest.mark.asyncio
async def test_invoke_builds_the_prompt_from_the_draft_source_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = SimpleNamespace(
        id="draft-1234",
        title_options_json=["一次版本发布说明"],
        summary_cn="摘要",
        source_item=SimpleNamespace(source_kind="rss"),
    )

    class FakeRepository:
        def get_draft(self, _draft_id: str):
            return draft

        def create_publication_asset(self, *_args):
            return SimpleNamespace(id="asset-1")

        def create_draft_illustration(self, *_args, **_kwargs):
            return SimpleNamespace(id="illustration-1")

    tool = image_generation.ImageGenerationTool.__new__(image_generation.ImageGenerationTool)
    tool.settings = SimpleNamespace(
        image_generation_enabled=True,
        image_generation_api_key="test-key",
        image_generation_ratio="4:3",
        image_generation_model="test-model",
        image_generation_provider="test-provider",
        generated_image_max_bytes=10 * 1024 * 1024,
    )
    tool.repository = FakeRepository()
    prompts: list[str] = []

    async def fake_generate(prompt: str):
        prompts.append(prompt)
        return b"image", "image/png"

    monkeypatch.setattr(tool, "_generate_without_cjk_text", fake_generate)
    monkeypatch.setattr(image_generation, "validate_image_attachment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        image_generation,
        "PrivateAttachmentStore",
        lambda _settings: SimpleNamespace(upload=lambda *_args: ("object-key", "sha256")),
    )

    illustration = await tool.invoke("draft-1234", "inline", 2, "段落二")
    brief = load_brief(SourceKind.RSS)
    assert brief is not None

    assert illustration.illustration_id == "illustration-1"
    # 风格来自 RSS brief 的风格池，主体是实物池条目（含位置 2 的上下文），而不是旧的通用方向描述。
    assert any(style in prompts[0] for style in brief.styles)
    assert any(item in prompts[0] for item in brief.objects)
    assert "段落二" in prompts[0]

    await tool.invoke(
        "draft-1234", "inline", 2, "段落二",
        "a wide deployment scene", "three blank tags hanging on a string",
        "Single-ink letterpress impression on thick cotton paper, straight-on view. Palette: charcoal on cream.",
    )

    assert "Subject: three blank tags hanging on a string." in prompts[1]
    assert prompts[1].startswith("Single-ink letterpress impression on thick cotton paper")


@pytest.mark.asyncio
async def test_image_generation_retries_when_ocr_detects_chinese_text(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = image_generation.ImageGenerationTool.__new__(image_generation.ImageGenerationTool)
    generated = [(b"first", "image/png"), (b"second", "image/png")]

    async def fake_generate(_prompt: str):
        return generated.pop(0)

    detections = [["中文标题"], []]
    monkeypatch.setattr(tool, "_generate", fake_generate)
    monkeypatch.setattr(image_generation, "detect_visible_text", lambda _content: detections.pop(0))

    content, content_type = await tool._generate_without_cjk_text("safe prompt")

    assert content == b"second"
    assert content_type == "image/png"


@pytest.mark.asyncio
async def test_image_generation_accepts_latin_and_numeric_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """字母与数字属于实物细节（尺子刻度、键帽、包装印刷），不再触发重试。"""
    tool = image_generation.ImageGenerationTool.__new__(image_generation.ImageGenerationTool)
    calls: list[str] = []

    async def fake_generate(prompt: str):
        calls.append(prompt)
        return b"first", "image/png"

    monkeypatch.setattr(tool, "_generate", fake_generate)
    monkeypatch.setattr(image_generation, "detect_visible_text", lambda _content: ["1 2 3 4", "USB-C 65W"])

    content, content_type = await tool._generate_without_cjk_text("safe prompt")

    assert content == b"first"
    assert content_type == "image/png"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_image_generation_rejects_image_when_every_ocr_check_finds_chinese(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = image_generation.ImageGenerationTool.__new__(image_generation.ImageGenerationTool)

    async def fake_generate(_prompt: str):
        return b"image", "image/png"

    monkeypatch.setattr(tool, "_generate", fake_generate)
    monkeypatch.setattr(image_generation, "detect_visible_text", lambda _content: ["乱码中文", "OK"])

    with pytest.raises(image_generation.ImageGenerationError, match="包含中文文字"):
        await tool._generate_without_cjk_text("safe prompt")


def test_cjk_text_only_flags_chinese_and_kana() -> None:
    assert image_text_guard.contains_cjk("编者按")
    assert image_text_guard.contains_cjk("全角ＡＢＣ")
    assert image_text_guard.contains_cjk("カタカナ")
    assert not image_text_guard.contains_cjk("USB-C 65W")
    assert not image_text_guard.contains_cjk("1 2 3 4 5")
    assert image_text_guard.cjk_text(["USB-C", "乱码", "12"]) == ["乱码"]


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
