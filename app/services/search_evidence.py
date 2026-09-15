"""联网补充证据的共用格式化：生成与改稿两条路径必须给模型同一段规则文本。

单独成模块的原因与 `source_refresh` / `evidence_search` 相同：避免工作流层反向导入工具层
（`auto_revision` 属于 tools，生成器属于 services）。
"""

from __future__ import annotations

# 生成与改稿共用的“联网补充”段落标题与约束。两边各写一份会漂移，规则一旦不一致，
# 同一份资料在两条路径下会被允许或被禁止用于不同用途。
SEARCH_EVIDENCE_RULES = (
    "联网补充资料（只能用于为正文里的外部名称与背景补一句准确说明，不得用于编造项目事实、"
    "不得据此改写来源事实，也不要把正文写成安装教程）："
)


def format_search_evidence_section(search_evidence: list[dict] | None, *, max_entries: int = 2) -> str:
    """把联网检索结果整理成提示词段落；没有资料时返回空串（保持原提示词不变）。"""
    if not search_evidence:
        return ""
    lines: list[str] = []
    for entry in search_evidence[:max_entries]:
        if not isinstance(entry, dict):
            continue
        title = " ".join(str(entry.get("title") or "").split())[:120]
        content = " ".join(str(entry.get("content") or "").split())[:1200]
        if not content:
            continue
        lines.append(f"- {title}：{content}" if title else f"- {content}")
    if not lines:
        return ""
    return SEARCH_EVIDENCE_RULES + "\n" + "\n".join(lines) + "\n"
