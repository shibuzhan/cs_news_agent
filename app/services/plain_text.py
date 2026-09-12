from __future__ import annotations

import re


WECHAT_DESCRIPTION_MAX_CHARS = 120
LOGICAL_SECTION_MIN_CHARS = (180, 240, 220, 180)
LOGICAL_SECTION_NAMES = ("背景与切入", "技术与过程", "价值与边界", "后续观察")
NATURAL_ARTICLE_MIN_PARAGRAPHS = 4
NATURAL_ARTICLE_MAX_PARAGRAPHS = 8
NATURAL_ARTICLE_MIN_CHARS = 1200


class LogicalSectionError(ValueError):
    """模型没有按可审计的四部分正文结构输出。"""


class NaturalArticleError(ValueError):
    """模型没有返回可发布前审核的自然段正文。"""


def compose_logical_sections(raw_sections: object) -> tuple[str, list[dict[str, int | str]]]:
    """合并四个不可见逻辑部分；每部分允许含多个自然段，不输出可见标题。"""
    if not isinstance(raw_sections, list) or len(raw_sections) != len(LOGICAL_SECTION_MIN_CHARS):
        raise LogicalSectionError("正文必须返回 4 个逻辑部分")
    sections: list[str] = []
    report: list[dict[str, int | str]] = []
    for index, (raw, minimum, name) in enumerate(
        zip(raw_sections, LOGICAL_SECTION_MIN_CHARS, LOGICAL_SECTION_NAMES, strict=True), start=1
    ):
        if not isinstance(raw, str):
            raise LogicalSectionError(f"第 {index} 个逻辑部分不是文本")
        normalized = normalize_plain_text(raw).strip()
        char_count = len(re.sub(r"\s+", "", normalized))
        if char_count < minimum:
            raise LogicalSectionError(f"{name}不足 {minimum} 字符")
        paragraphs = [item for item in normalized.split("\n\n") if item.strip()]
        if not paragraphs:
            raise LogicalSectionError(f"{name}缺少正文")
        sections.append(normalized)
        report.append({
            "index": index,
            "name": name,
            "min_chars": minimum,
            "char_count": char_count,
            "paragraph_count": len(paragraphs),
        })
    return "\n\n".join(sections), report


def compose_natural_article(
    raw_body: object,
    min_chars: int = NATURAL_ARTICLE_MIN_CHARS,
) -> tuple[str, dict[str, int]]:
    """规范自然正文，不要求模型输出固定逻辑部分数组。"""
    if not isinstance(raw_body, str):
        raise NaturalArticleError("正文必须返回 body 文本")
    normalized = normalize_plain_text(raw_body).strip()
    paragraphs = [item.strip() for item in normalized.split("\n\n") if item.strip()]
    if not NATURAL_ARTICLE_MIN_PARAGRAPHS <= len(paragraphs) <= NATURAL_ARTICLE_MAX_PARAGRAPHS:
        raise NaturalArticleError(
            f"正文应为 {NATURAL_ARTICLE_MIN_PARAGRAPHS} 到 {NATURAL_ARTICLE_MAX_PARAGRAPHS} 个自然段"
        )
    body = "\n\n".join(paragraphs)
    body_chars = len(re.sub(r"\s+", "", body))
    if body_chars < min_chars:
        raise NaturalArticleError(f"正文至少需要 {min_chars} 个字符")
    return body, {
        "paragraph_count": len(paragraphs),
        "body_chars": body_chars,
    }


def normalize_plain_text(value: str) -> str:
    """移除常见 Markdown/HTML 标记，保留可审核的中文纯文本与段落。"""
    lines: list[str] = []
    blank_pending = False
    for raw in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            if lines:
                blank_pending = True
            continue
        if blank_pending:
            lines.append("")
            blank_pending = False
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"<[^>]+>", "", line)
        line = line.replace("**", "").replace("__", "").replace("`", "").strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def normalize_wechat_description(value: str) -> str:
    """将公众号 description 收敛为单句，避免草稿接口因长度拒绝。"""
    normalized = normalize_plain_text(value)
    one_line = re.sub(r"\s+", " ", normalized).strip()
    if not one_line:
        return "值得关注的科技资讯，点击查看原始信息与解读。"
    if len(one_line) <= WECHAT_DESCRIPTION_MAX_CHARS:
        return one_line
    return one_line[: WECHAT_DESCRIPTION_MAX_CHARS - 1].rstrip("，、；：。 ") + "…"


def format_source_body(body: str, title: str, source_kind: str | None = None) -> str:
    """统一正文缩进与来源尾注；GitHub 项目名已在标题出现，不重复附原文标题。"""
    kept_lines = [
        line
        for line in normalize_plain_text(body).splitlines()
        if not line.startswith("原文标题：")
        and not line.startswith("原文链接：")
        and not line.startswith("来源链接：")
    ]
    paragraphs = [
        paragraph.strip()
        for paragraph in "\n".join(kept_lines).strip().split("\n\n")
        if paragraph.strip()
    ]
    content = "\n\n".join(
        "　　" + paragraph.lstrip("　 ") for paragraph in paragraphs
    )
    if str(source_kind or "").casefold() == "github":
        return content
    footer = f"原文标题：{title}"
    return f"{content}\n\n{footer}" if content else footer


def append_source_title(body: str, title: str) -> str:
    """兼容既有非 GitHub 来源的正文来源尾注。"""
    return format_source_body(body, title)
