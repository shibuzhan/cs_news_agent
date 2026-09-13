"""预览必须与实际投递用同一套尾注/结尾图规则。

真实反馈：“我不是要求文末用图片取代原本的点击查看原文跳转项目地址文字吗？为什么还是这样”
——远端草稿是对的（无文字尾注），但**应用内预览**直接按空行渲染 `draft.body`，
既不认识尾注行、也不显示固定结尾图，于是看起来像没生效。
"""

from __future__ import annotations

import inspect
import pathlib

from app.services import generator as generator_module  # noqa: F401  (确保模块可用)
from app.services.plain_text import GITHUB_SOURCE_HINT, SOURCE_FOOTER_PREFIXES, is_source_footer_line
from app.services.wechat_official import render_wechat_html

APP_SOURCE = pathlib.Path("frontend/src/App.tsx").read_text(encoding="utf-8")
API_SOURCE = pathlib.Path("frontend/src/api.ts").read_text(encoding="utf-8")

FOOTER_URL = "https://mmbiz.qpic.cn/sz_mmbiz_png/footer/0?from=appmsg"


def test_github_hint_is_recognised_as_footer_line() -> None:
    assert GITHUB_SOURCE_HINT in SOURCE_FOOTER_PREFIXES
    assert is_source_footer_line(f"　　{GITHUB_SOURCE_HINT}")
    assert not is_source_footer_line("这是正文段落")


def test_renderer_drops_text_footer_when_footer_image_is_set() -> None:
    body = f"第一段正文。\n\n第二段正文。\n\n{GITHUB_SOURCE_HINT}"

    with_image = render_wechat_html(body, [], footer_image_url=FOOTER_URL, include_text_footer=False)
    without = render_wechat_html(body, [], footer_image_url=None, include_text_footer=True)

    assert GITHUB_SOURCE_HINT not in with_image     # 文字尾注被结尾图取代
    assert FOOTER_URL in with_image                 # 结尾图追加在文末
    assert with_image.rstrip().endswith("</p>")
    assert GITHUB_SOURCE_HINT in without            # 未配置结尾图时保持原样


def test_frontend_preview_mirrors_the_same_rules() -> None:
    # 预览要用后端返回的尾注前缀，而不是自己写死一份
    assert "footer_text_prefixes" in APP_SOURCE
    assert "hideFooterText" in APP_SOURCE
    assert "footer_image_download_url" in APP_SOURCE     # 文末渲染固定结尾图
    assert "getPublicationPreferences" in APP_SOURCE     # 数据来自新接口
    assert 'request<PublicationPreferences>("/wechat/publication-preferences")' in API_SOURCE


def test_preview_order_is_note_title_summary_cover_body() -> None:
    """版式顺序：统计说明（标题上方）→ 标题 → 摘要 → 封面 → 正文 → 结尾图。"""
    section = APP_SOURCE[APP_SOURCE.index("function PublicationArticlePreview(") :]
    section = section[: section.index("function PublishingPage(")]

    note_at = section.index("草稿共 ${illustrations.length} 张插图")
    title_at = section.index("<h2>{draft.title_options[0]}</h2>")
    summary_at = section.index('className="preview-summary"')
    cover_at = section.index('className="publication-preview-cover"')
    body_at = section.index('className="preview-body"')
    footer_at = section.index("footer_image_download_url")

    assert note_at < title_at < summary_at < cover_at < body_at < footer_at


def test_preview_endpoint_exposes_what_the_frontend_needs() -> None:
    from app.api import routes

    source = inspect.getsource(routes.read_publication_preferences)

    for field in (
        "cover_in_body",
        "footer_text_enabled",
        "footer_image_configured",
        "footer_image_download_url",
        "footer_text_prefixes",
    ):
        assert field in source
