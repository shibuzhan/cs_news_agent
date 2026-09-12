"""来源图片链接不能在规范化时丢失。

真实故障：`affaan-m/ECC` 的 README 里有 35 个 `<img>`（横幅 assets/hero.png、8 个 `<picture>`），
但 `BeautifulSoup.get_text()` 会把没有文本内容的 `<img>` 整段丢掉，于是入库的正文里
图片链接为 0，选图与"用真实截图"的链路全都拿不到图。
"""

from __future__ import annotations

from app.services.normalizer import clean_text, preserve_image_links
from app.services.source_media import extract_image_urls, raw_github_base


def test_html_image_tags_become_markdown_links() -> None:
    html = '<p align="center"><img src="assets/hero.png" width="100%" alt="hero" /></p>'

    converted = preserve_image_links(html)

    assert "![](assets/hero.png)" in converted


def test_picture_source_srcset_is_preserved() -> None:
    html = '<picture><source srcset="assets/dark.png 1x, assets/dark@2x.png 2x"><img src="assets/light.png"></picture>'

    converted = preserve_image_links(html)

    assert "assets/dark.png" in converted
    assert "assets/light.png" in converted


def test_clean_text_keeps_image_links_and_still_strips_other_tags() -> None:
    raw = '<div><h1>标题</h1><img src="./docs/shot.png"><b>加粗</b></div>'

    cleaned = clean_text(raw, 5000, keep_newlines=True)

    assert "docs/shot.png" in cleaned          # 图片链接保留
    assert "<div>" not in cleaned and "<b>" not in cleaned  # 其它标签照旧剥掉
    assert "标题" in cleaned and "加粗" in cleaned


def test_extraction_works_on_normalized_github_readme() -> None:
    """规范化之后仍能提取到候选图（此前为 0）。"""
    readme_like = "\n\n".join(
        [
            '# Project\n\n<p align="center"><img src="assets/hero.png" width="100%" /></p>',
            "## Screenshots\n\n![demo](docs/demo.jpg)",
        ]
    )

    cleaned = clean_text(readme_like, 100000, keep_newlines=True)
    urls = [item.url for item in extract_image_urls(cleaned, base_url=raw_github_base("https://github.com/o/r"))]

    assert "https://raw.githubusercontent.com/o/r/HEAD/assets/hero.png" in urls
    assert "https://raw.githubusercontent.com/o/r/HEAD/docs/demo.jpg" in urls
