"""公众号草稿的留言设置必须显式下发。

真实反馈：“为什么上传的草稿留言功能都是关闭的？”
原因：建草稿的 article 里没有 `need_open_comment` / `only_fans_can_comment`，
微信在这两个字段缺省时按 **0（关闭留言）** 处理。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.tools.wechat_official_account import WechatOfficialAccountTool


def _tool(**overrides) -> WechatOfficialAccountTool:
    values = {"wechat_open_comment": True, "wechat_only_fans_can_comment": False}
    values.update(overrides)
    return WechatOfficialAccountTool(SimpleNamespace(**values))  # type: ignore[arg-type]


def test_article_payload_opens_comments_by_default() -> None:
    payload = _tool()._article_payload(
        title="标题", digest="摘要", content_html="<p>正文</p>",
        source_url="https://example.com", cover_media_id="cover-media",
    )

    assert payload["need_open_comment"] == 1
    assert payload["only_fans_can_comment"] == 0
    assert payload["show_cover_pic"] == 1
    assert payload["thumb_media_id"] == "cover-media"


def test_comment_settings_follow_configuration() -> None:
    tool = _tool(wechat_open_comment=False, wechat_only_fans_can_comment=True)

    payload = tool._article_payload(
        title="标题", digest="摘要", content_html="<p>正文</p>",
        source_url="https://example.com", cover_media_id="cover-media",
    )

    assert payload["need_open_comment"] == 0
    assert payload["only_fans_can_comment"] == 1


def test_both_create_and_update_use_the_same_payload() -> None:
    import inspect

    source = inspect.getsource(WechatOfficialAccountTool)

    assert source.count("self._article_payload(") >= 2  # create + update
