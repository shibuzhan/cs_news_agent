"""来源图片：从 README / 官方页面 markdown 中提取图片链接，并做下载前的校验。

只解决两件事：
1. 从 markdown（含 HTML `<img>`）里挑出**可能是内容截图**的图片链接——徽章、追踪像素、赞助图一律排除；
2. 下载后校验类型与体积，返回可直接入库的字节。

版权边界（见 agent_skills/source-media）：只允许来源仓库自带的图与项目官方页面/文档页的图，
并在草稿里标注来源；第三方文章里的图不使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse


# markdown ![alt](url "title") 与 HTML <img src="...">
_MD_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<url><[^>]+>|[^)\s]+)")
_HTML_IMAGE = re.compile(r"<img[^>]+src=[\"'](?P<url>[^\"']+)[\"'][^>]*>", re.IGNORECASE)
# README 里绝大多数图片链接是徽章/统计图，不是内容截图。
_NOISE_HINTS = (
    "shields.io", "badge", "badgen", "travis-ci", "circleci", "codecov", "coveralls",
    "sonarcloud", "snyk.io", "opencollective", "patreon", "paypal", "buymeacoffee",
    "ko-fi", "sponsor", "star-history", "starchart", "visitor", "counter", "hits.sh",
    "contrib.rocks", "gitstars", "trendshift", "awesome.re", "img.shields", "flat-square",
)
_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}
# 公众号正文不支持 SVG；GIF 虽支持，但自动截图默认不用动图。
_BLOCKED_SUFFIXES = (".svg", ".gif", ".mp4", ".mov", ".webm", ".ico")
MIN_IMAGE_BYTES = 8 * 1024


@dataclass(frozen=True)
class SourceImage:
    """一条候选图片来源。"""

    url: str
    alt: str
    origin: str  # readme | official_site

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower()


@dataclass(frozen=True)
class DownloadedImage:
    content: bytes
    content_type: str
    filename: str


class SourceImageError(ValueError):
    """图片不可用（不可下载、类型不支持或体积不合适）。"""


def _is_noise(url: str, alt: str) -> bool:
    lowered = f"{url} {alt}".casefold()
    return any(hint in lowered for hint in _NOISE_HINTS)


def _normalize(url: str) -> str:
    value = url.strip().strip("<>").strip()
    # 去掉 GitHub 的图片代理包装，保留原始地址。
    if value.startswith("https://camo.githubusercontent.com/"):
        return value
    return value


def raw_github_base(source_url: object) -> str:
    """GitHub 仓库地址 → README 里相对图片路径的真实基址。

    参数可能是 HttpUrl（Pydantic 字段）或字符串，统一转成文本再解析。
    """
    match = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+)/?$", str(source_url or "").strip(), re.IGNORECASE)
    if not match:
        return ""
    owner, repo = match.group(1), match.group(2).removesuffix(".git")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/"


def extract_image_urls(text: str, *, base_url: str = "", origin: str = "readme", limit: int = 40) -> list[SourceImage]:
    """从 markdown / HTML 文本里提取候选图片链接（去噪、去重、按出现顺序）。"""
    found: list[SourceImage] = []
    seen: set[str] = set()
    for match in list(_MD_IMAGE.finditer(text or "")) + list(_HTML_IMAGE.finditer(text or "")):
        url = _normalize(match.group("url"))
        if not url or url.startswith("data:"):
            continue
        if base_url:
            url = urljoin(base_url, url)
        if not url.startswith(("http://", "https://")):
            continue
        if url.casefold().endswith(_BLOCKED_SUFFIXES):
            continue
        alt = (match.groupdict().get("alt") or "").strip()[:120]
        if _is_noise(url, alt):
            continue
        if url in seen:
            continue
        seen.add(url)
        found.append(SourceImage(url=url, alt=alt, origin=origin))
        if len(found) >= limit:
            break
    return found


def image_filename(url: str, content_type: str) -> str:
    suffix = _CONTENT_TYPES.get(content_type.split(";")[0].strip().lower(), ".png")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", urlparse(url).path.rsplit("/", 1)[-1] or "source-image")[:60]
    # 扩展名一律按探测到的真实类型归一化（避免 .PNG / 无扩展名 / 错误扩展名）。
    if "." in stem.lstrip("."):
        stem = stem.rsplit(".", 1)[0]
    return f"{stem or 'source-image'}{suffix}"


def validate_downloaded_image(url: str, content: bytes, content_type: str, *, max_bytes: int) -> DownloadedImage:
    """下载后的校验：类型必须受支持、体积合理、确实是图片（按魔数判断）。"""
    normalized_type = (content_type or "").split(";")[0].strip().lower()
    if normalized_type not in _CONTENT_TYPES:
        raise SourceImageError(f"不支持的图片类型：{normalized_type or '未知'}")
    if len(content) > max_bytes:
        raise SourceImageError("图片超过大小上限，已跳过")
    if len(content) < MIN_IMAGE_BYTES:
        raise SourceImageError("图片过小（可能是徽章或追踪像素），已跳过")
    if not _looks_like_image(content, normalized_type):
        raise SourceImageError("返回内容不是有效图片，已跳过")
    return DownloadedImage(content=content, content_type=normalized_type, filename=image_filename(url, normalized_type))


def _looks_like_image(content: bytes, content_type: str) -> bool:
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False
