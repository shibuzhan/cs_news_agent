from __future__ import annotations

import re
from dataclasses import dataclass


WECHAT_DESCRIPTION_MAX_CHARS = 120
LOGICAL_SECTION_MIN_CHARS = (180, 240, 220, 180)
LOGICAL_SECTION_NAMES = ("背景与切入", "技术与过程", "价值与边界", "后续观察")
NATURAL_ARTICLE_MIN_PARAGRAPHS = 4
NATURAL_ARTICLE_MAX_PARAGRAPHS = 8
NATURAL_ARTICLE_MIN_CHARS = 1200
# 证据正文的挑选参数：开头概览预算，以及与“背景/能力/用法”相关或无关的信号词。
_EVIDENCE_HEAD_CHARS = 1500
# 单个证据块的上限：超过就按行再切，避免"一整篇当成一段"导致预算失效。
_EVIDENCE_BLOCK_MAX_CHARS = 1600
_EVIDENCE_POSITIVE_HINTS = (
    "overview", "what is", "what it", "feature", "capabilit", "how it works", "architecture",
    "workflow", "usage", "quick start", "getting started", "example", "concept", "design goal",
    "why ", "use case", "benefit",
)
_EVIDENCE_NEGATIVE_HINTS = (
    "badge", "shields.io", "language:", "license", "contributing", "code of conduct", "changelog",
    "release note", "pricing", "sponsor", "npm install", "npm i ", "yarn add", "pnpm add",
    "pip install", "brew install", "table of contents", "star history", "acknowledg",
    "third-party re-uploads",
)
# GitHub 文案的文末固定提示（来源地址由公众号“阅读原文”承载）。
GITHUB_SOURCE_HINT = "点击查看原文跳转项目地址"
# 正文末尾的来源性行：不计入自然段、不加段首缩进、不算正文长度。
# 公开为 `SOURCE_FOOTER_PREFIXES`：渲染、审核与**前端预览**必须用同一套判定，
# 否则会出现“预览里还有文字尾注、投递出去的却没有”。
SOURCE_FOOTER_PREFIXES = ("原文标题：", "原文链接：", "来源链接：", GITHUB_SOURCE_HINT)
_SOURCE_FOOTER_PREFIXES = SOURCE_FOOTER_PREFIXES
# 外部名称候选：1 到 3 个拉丁字母词，首词大写（Claude Code、OpenCode、AgentShield…）。
_NAME_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[.+#-][A-Za-z0-9]+)*(?:\s+[A-Z][A-Za-z0-9]*(?:[.+#-][A-Za-z0-9]+)*){0,2}\b")
# 常见英文虚词与目标读者已知的通用技术语汇，不作为需要联网说明的名称。
_NAME_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "you", "your", "not", "are", "was",
        "json", "api", "sdk", "cli", "url", "http", "https", "html", "css", "sql", "yaml", "toml",
        "ai", "llm", "mcp", "gpu", "cpu", "ram", "os", "ui", "ux", "ide", "vscode", "github",
        "git", "npm", "node js", "node", "python", "docker", "linux", "macos", "windows", "readme",
        "mit", "apache", "contributing", "changelog", "trending", "agent", "agents", "skills", "skill",
        "memory", "security", "hooks", "rules", "prompt", "prompts", "token", "tokens", "star", "stars",
        "fork", "forks", "commit", "commits", "pull request", "issue", "issues", "readme md",
    }
)


def article_length_band(min_chars: int, max_chars: int) -> tuple[int, int, int, int]:
    """返回正文写作的字数区间：目标下限、目标上限、硬下限、硬上限。

    目标带明显低于硬上限，避免模型每次都顶到上限附近（历史上出现过模型稳定写到 3860 字、
    而规则上限 3200 导致审核永远不通过的循环）。
    """
    minimum = max(min_chars, NATURAL_ARTICLE_MIN_CHARS)
    maximum = max(max_chars, minimum)
    target_low = min(maximum, minimum + 400)
    target_high = min(maximum, minimum + 1000)
    return target_low, target_high, minimum, maximum


@dataclass(frozen=True)
class MarkdownSection:
    """来源正文里的一个章节；`title` 为空表示第一个标题之前的开头部分。"""

    title: str
    level: int
    body: str

    @property
    def text(self) -> str:
        if not self.title:
            return self.body
        return f"{'#' * max(self.level, 1)} {self.title}\n\n{self.body}".strip()


_HEADING_LINE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.M)


def split_markdown_sections(text: str) -> list[MarkdownSection]:
    """按 Markdown 标题切分来源正文；没有标题时返回空列表（由调用方回退到块挑选）。"""
    matches = list(_HEADING_LINE.finditer(text or ""))
    if not matches:
        return []
    sections: list[MarkdownSection] = []
    intro = (text[: matches[0].start()] or "").strip()
    if intro:
        sections.append(MarkdownSection(title="", level=0, body=intro))
    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append(
            MarkdownSection(
                title=match.group(2).strip(),
                level=len(match.group(1)),
                body=(text[body_start:body_end] or "").strip(),
            )
        )
    return sections


def _split_evidence_blocks(text: str) -> list[str]:
    """把来源正文切成可挑选的块：先按空行分段，过长的段再按行、最后按长度硬切。

    很多 README 通篇没有空行，如果只按空行分段就会得到"一个 8 万字的段落"，
    任何预算控制都会失效（历史上正是因此把整篇原文原样喂给了模型）。
    """
    blocks: list[str] = []
    for raw in re.split(r"\n\s*\n", text):
        stripped = raw.strip()
        if not stripped:
            continue
        if len(stripped) <= _EVIDENCE_BLOCK_MAX_CHARS:
            blocks.append(stripped)
            continue
        buffer = ""
        for line in stripped.split("\n"):
            line = line.strip()
            if not line:
                continue
            if buffer and len(buffer) + len(line) + 1 > _EVIDENCE_BLOCK_MAX_CHARS:
                blocks.append(buffer)
                buffer = line
            else:
                buffer = f"{buffer}\n{line}" if buffer else line
        if buffer:
            blocks.append(buffer)
    sliced: list[str] = []
    for block in blocks:
        while len(block) > _EVIDENCE_BLOCK_MAX_CHARS:
            window = block[:_EVIDENCE_BLOCK_MAX_CHARS]
            cut = max(window.rfind("\n"), window.rfind(" | "))
            if cut < _EVIDENCE_BLOCK_MAX_CHARS // 2:
                cut = _EVIDENCE_BLOCK_MAX_CHARS
            sliced.append(window[:cut].strip())
            block = block[cut:].lstrip()
        if block:
            sliced.append(block)
    return [item for item in sliced if item]


def select_evidence_text(content: str, budget: int) -> str:
    """按相关度挑选来源正文，而不是直接截取开头。

    大型 README 的开头往往是语言清单、官方渠道与安装警告；直接砍前 N 字会让文章照着
    这些内容写。这里保留开头概览，再按“背景/能力/用法”相关度补充正文段落，跳过安装、
    价格、贡献指南与更新日志这类内容，总长不超过 `budget`。
    """
    text = (content or "").replace("\r", "")
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    blocks = _split_evidence_blocks(text)
    if not blocks:
        return text[:budget]
    head_budget = min(_EVIDENCE_HEAD_CHARS, budget // 4)
    head: list[str] = []
    used = 0
    for block in blocks:
        if used + len(block) > head_budget:
            break
        head.append(block)
        used += len(block)
    scored: list[tuple[int, int, str]] = []
    for index, block in enumerate(blocks[len(head):], start=len(head)):
        lowered = block.casefold()
        score = sum(2 for hint in _EVIDENCE_POSITIVE_HINTS if hint in lowered)
        score -= sum(3 for hint in _EVIDENCE_NEGATIVE_HINTS if hint in lowered)
        if 200 <= len(block) <= 2000:
            score += 1
        if block.startswith(("|", "```", ">")):
            score -= 1
        scored.append((score, index, block))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = list(head)
    for score, _index, block in scored:
        if score <= 0 or used + len(block) + 2 > budget:
            continue
        chosen.append(block)
        used += len(block) + 2
    if len(chosen) == len(head):
        # 没有任何正相关块时，按原顺序补齐，避免证据为空。
        for block in blocks[len(head):]:
            if used + len(block) + 2 > budget:
                break
            chosen.append(block)
            used += len(block) + 2
    if not chosen:
        return text[:budget]
    return "\n\n".join(chosen)[:budget]


def is_source_footer_line(line: str) -> bool:
    """判断是否为来源尾注/文末提示行；审核、配图与渲染共用同一判定。"""
    stripped = line.strip()
    return any(stripped.startswith(prefix) for prefix in _SOURCE_FOOTER_PREFIXES)


def extract_name_queries(text: str, limit: int = 2) -> list[str]:
    """从正文里提取最可能需要联网说明的外部名称（纯启发式，不调用模型）。

    只挑拉丁字母构成的产品/工具/组织名（1 到 3 个词、首词大写），排除常见英文虚词与
    目标读者已知的通用技术语汇；按出现次数排序，最多返回 `limit` 条。
    """
    if not text:
        return []
    candidates: dict[str, int] = {}
    for match in _NAME_PATTERN.finditer(text):
        phrase = " ".join(match.group(0).split())
        if len(phrase) < 3 or phrase.casefold() in _NAME_STOPWORDS:
            continue
        candidates[phrase] = candidates.get(phrase, 0) + 1
    ordered = sorted(candidates.items(), key=lambda item: (-item[1], text.find(item[0])))
    return [name for name, _count in ordered[: max(limit, 0)]]


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
    """统一正文缩进与来源尾注；GitHub 项目名已在标题出现，改为追加“阅读原文”提示。"""
    kept_lines = [
        line
        for line in normalize_plain_text(body).splitlines()
        if not is_source_footer_line(line)
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
        # GitHub 文案不再重复原文标题：项目名已在标题与正文开头出现，
        # 文末用一句固定提示指向公众号“阅读原文”里的项目地址。
        return f"{content}\n\n{GITHUB_SOURCE_HINT}" if content else GITHUB_SOURCE_HINT
    footer = f"原文标题：{title}"
    return f"{content}\n\n{footer}" if content else footer


def append_source_title(body: str, title: str) -> str:
    """兼容既有非 GitHub 来源的正文来源尾注。"""
    return format_source_body(body, title)
