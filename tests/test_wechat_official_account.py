from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.wechat_official import (
    IMAGE_PARAGRAPH_STYLE,
    PARAGRAPH_STYLE,
    WechatOfficialAccountError,
    remote_drafts_from_payload,
    render_wechat_html,
)
from app.tools import wechat_official_account
from app.tools.wechat_official_account import WechatOfficialAccountTool


def test_render_wechat_html_escapes_draft_text_and_normalizes_wechat_inline_images() -> None:
    rendered = render_wechat_html(
        "# 标题\n普通内容 <script>alert(1)</script>",
        [
            "https://mmbiz.qpic.cn/inline.png",
            "http://mmbiz.qpic.cn/returned-by-wechat.png?from=appmsg",
            "http://not-inserted.example/image.png",
        ],
    )

    assert "<p>　　标题</p>" not in rendered
    assert "　　标题</p>" in rendered
    assert "<h1>" not in rendered
    assert "<script>" not in rendered
    assert "alert(1)" in rendered
    assert 'src="https://mmbiz.qpic.cn/inline.png"' in rendered
    assert 'src="https://mmbiz.qpic.cn/returned-by-wechat.png?from=appmsg"' in rendered
    assert "not-inserted.example" not in rendered


def test_render_wechat_html_keeps_paragraph_spacing_inline() -> None:
    """段间距必须写成内联样式：微信草稿编辑器默认把相邻段落贴在一起（用户反馈“空行没了”）。"""
    rendered = render_wechat_html("第一段\n\n第二段\n\n点击查看原文跳转项目地址", [])

    assert rendered.count(f'<p style="{PARAGRAPH_STYLE}">') == 3
    assert "margin:0 0 22px" in rendered and "line-height:1.75" in rendered
    # 图片段落用更小的下间距，且不让 img 带上行高留白。
    with_image = render_wechat_html("第一段", ["https://mmbiz.qpic.cn/a.png"])
    assert f'<p style="{IMAGE_PARAGRAPH_STYLE}">' in with_image


def test_remote_draft_payload_is_reduced_to_safe_display_fields() -> None:
    total, items = remote_drafts_from_payload(
        {
            "total_count": 1,
            "item": [
                {
                    "media_id": "draft-media-1",
                    "update_time": 1_725_000_000,
                    "content": {"news_item": [{"title": "第一篇草稿", "author": "资讯运营 Agent"}]},
                }
            ],
        }
    )

    assert total == 1
    assert items[0].media_id == "draft-media-1"
    assert items[0].title == "第一篇草稿"
    assert items[0].author == "资讯运营 Agent"
    assert items[0].updated_at


@pytest.mark.asyncio
async def test_tool_uses_local_skill_api_and_does_not_precheck_inline_size(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []

    class SkillError(RuntimeError):
        def __init__(self, message: str, *, code: int | None = None) -> None:
            super().__init__(message)
            self.code = code

    class FakeApi:
        def __init__(self, app_id: str, app_secret: str, *, timeout_seconds: float) -> None:
            assert (app_id, app_secret, timeout_seconds) == ("app-id", "app-secret", 31)

        async def aclose(self) -> None:
            return None

        async def upload_permanent_image(self, content: bytes, filename: str) -> str:
            calls.append((filename, len(content)))
            return "cover-media-id"

        async def upload_inline_image(self, content: bytes, filename: str) -> str:
            calls.append((filename, len(content)))
            return "https://mmbiz.qpic.cn/inline.png"

        async def create_draft(self, article: dict[str, object]) -> str:
            assert article["thumb_media_id"] == "cover-media-id"
            assert article["content_source_url"] == "https://example.com/source"
            assert "contentSourceUrl" not in article
            return "draft-media-id"

        async def update_draft(self, media_id: str, article: dict[str, object]) -> None:
            assert media_id == "draft-media-id"
            assert article["thumb_media_id"] == "cover-media-id"

        async def list_drafts(self, *, offset: int, count: int) -> dict[str, object]:
            assert (offset, count) == (0, 20)
            return {"total_count": 0, "item": []}

    monkeypatch.setattr(
        wechat_official_account,
        "_load_skill_module",
        lambda: SimpleNamespace(WechatOfficialAccountApi=FakeApi, WechatOfficialAccountError=SkillError),
    )
    settings = Settings(wechat_app_id="app-id", wechat_app_secret="app-secret", wechat_api_timeout_seconds=31)
    async with WechatOfficialAccountTool(settings) as tool:
        assert await tool.upload_cover(b"cover", "cover.png") == "cover-media-id"
        assert await tool.upload_inline_image(b"x" * (2 * 1024 * 1024), "large.png") == "https://mmbiz.qpic.cn/inline.png"
        assert await tool.create_draft(
            title="测试文章",
            digest="测试摘要",
            content_html="<p>正文</p>",
            source_url="https://example.com/source",
            cover_media_id="cover-media-id",
        ) == "draft-media-id"
        await tool.update_draft(
            media_id="draft-media-id",
            title="测试文章",
            digest="测试摘要",
            content_html="<p>正文</p>",
            source_url="https://example.com/source",
            cover_media_id="cover-media-id",
        )
        assert await tool.list_remote_drafts() == (0, [])
    assert calls == [("cover.png", 5), ("large.png", 2 * 1024 * 1024)]


@pytest.mark.asyncio
async def test_tool_hides_wechat_ip_from_whitelist_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class SkillError(RuntimeError):
        def __init__(self, message: str, *, code: int | None = None) -> None:
            super().__init__(message)
            self.code = code

    class FakeApi:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def list_drafts(self, **_kwargs: object) -> dict[str, object]:
            raise SkillError("invalid ip 103.172.183.79", code=40164)

    monkeypatch.setattr(
        wechat_official_account,
        "_load_skill_module",
        lambda: SimpleNamespace(WechatOfficialAccountApi=FakeApi, WechatOfficialAccountError=SkillError),
    )
    async with WechatOfficialAccountTool(Settings(wechat_app_id="app-id", wechat_app_secret="app-secret")) as tool:
        with pytest.raises(WechatOfficialAccountError) as caught:
            await tool.list_remote_drafts()
    assert caught.value.category == "wechat_ip_whitelist"
    assert "当前出口 IP：103.172.183.79" in str(caught.value)
    assert "IP 白名单" in str(caught.value)
