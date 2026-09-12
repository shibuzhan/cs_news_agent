"""来源图片：提取、去噪与下载校验（不联网，纯函数）。"""

from __future__ import annotations

import pytest

from app.services.source_media import (
    SourceImageError,
    extract_image_urls,
    image_filename,
    validate_downloaded_image,
)


README = """
# Project

[![CI](https://github.com/o/r/actions/workflows/ci.yml/badge.svg)](https://github.com/o/r/actions)
![stars](https://img.shields.io/github/stars/o/r)
![Sponsor](https://img.shields.io/badge/sponsor-♥-ff69b4)

## Screenshots

![Dashboard overview](./docs/screenshots/dashboard.png)
<img src="https://raw.githubusercontent.com/o/r/main/docs/demo.jpg" alt="demo" />
![Tracking pixel](https://example.com/1x1.gif)
![Icon](https://example.com/logo.svg)
"""


def test_extract_keeps_content_screenshots_and_drops_badges() -> None:
    from app.services.source_media import raw_github_base

    base = raw_github_base("https://github.com/o/r")
    images = extract_image_urls(README, base_url=base, origin="readme")
    urls = [item.url for item in images]

    assert "https://raw.githubusercontent.com/o/r/HEAD/docs/screenshots/dashboard.png" in urls  # 相对路径解析到 raw 基址
    assert "https://raw.githubusercontent.com/o/r/main/docs/demo.jpg" in urls
    # 徽章、赞助图、动图、SVG、追踪像素都不应进入候选。
    assert all("shields.io" not in url for url in urls)
    assert all(not url.endswith((".gif", ".svg")) for url in urls)
    assert images[0].origin == "readme"
    assert images[0].domain in {"github.com", "raw.githubusercontent.com"}


def test_extract_deduplicates_and_ignores_data_uris() -> None:
    text = "![a](https://cdn.example.com/a.png)\n![a again](https://cdn.example.com/a.png)\n![x](data:image/png;base64,AAAA)"
    images = extract_image_urls(text, origin="official_site")

    assert [item.url for item in images] == ["https://cdn.example.com/a.png"]
    assert images[0].origin == "official_site"


def test_validate_accepts_png_and_rejects_bad_payloads() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20_000

    downloaded = validate_downloaded_image(
        "https://cdn.example.com/shot.png", png, "image/png", max_bytes=1_000_000
    )
    assert downloaded.content == png and downloaded.filename.endswith(".png")

    with pytest.raises(SourceImageError):
        validate_downloaded_image("https://x/a.svg", png, "image/svg+xml", max_bytes=1_000_000)
    with pytest.raises(SourceImageError):
        validate_downloaded_image("https://x/small.png", png[:100], "image/png", max_bytes=1_000_000)
    with pytest.raises(SourceImageError):
        validate_downloaded_image("https://x/fake.png", b"<html>not an image</html>" + b"x" * 20_000, "image/png", max_bytes=1_000_000)
    with pytest.raises(SourceImageError):
        validate_downloaded_image("https://x/big.png", png, "image/png", max_bytes=1024)


def test_raw_github_base_accepts_url_objects_and_variants() -> None:
    from pydantic import HttpUrl

    from app.services.source_media import raw_github_base

    assert raw_github_base("https://github.com/o/r") == "https://raw.githubusercontent.com/o/r/HEAD/"
    assert raw_github_base("https://github.com/o/r.git/") == "https://raw.githubusercontent.com/o/r/HEAD/"
    # 草稿的 source_url 是 Pydantic HttpUrl，不能假设是字符串。
    assert raw_github_base(HttpUrl("https://github.com/o/r")) == "https://raw.githubusercontent.com/o/r/HEAD/"
    assert raw_github_base("https://example.com/a/b") == ""
    assert raw_github_base(None) == ""


def test_image_filename_normalizes_extension() -> None:
    assert image_filename("https://cdn.example.com/path/Dashboard.PNG?x=1", "image/png").endswith(".png")
    assert image_filename("https://cdn.example.com/path/demo", "image/jpeg").endswith(".jpg")


def test_source_media_tools_are_registered() -> None:
    from app.agent_tools.source_media_tools import build_source_media_tools

    names = {item.name for item in build_source_media_tools("session-1")}

    assert names == {"list_source_images", "attach_source_image"}
