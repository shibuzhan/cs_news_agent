"""面向科技资讯读者的术语说明边界。"""

from __future__ import annotations


# 这些词已是目标读者的常用工作语汇。强行括号解释会让文章显得生硬，
# 也会诱发审核模型反复挑出并不构成问题的“缺少解释”。
COMMON_TECH_TERMS = (
    "AI", "AI agent", "agent", "GitHub", "GitHub Trending", "vibe coding",
    "coding", "API", "SDK", "LLM", "MCP", "Docker", "README", "开源项目",
)


def terminology_guidance() -> str:
    """供生成、改稿和审核共用，避免三个模型执行互相矛盾的术语规则。"""
    examples = "、".join(COMMON_TECH_TERMS)
    return (
        "术语说明只用于来源关键且普通科技读者可能不熟悉的生僻概念；"
        "首次出现时保留原术语，并给出不超过一句、且受来源证据支持的简短解释。"
        f"以下属于常见技术语汇，不得仅因首次出现而要求或补充解释：{examples}。"
        "项目名、仓库名、产品名应原样保留；除非它本身是理解正文所必需的陌生概念，"
        "不要把名称拆开翻译或强制加括号说明。"
    )
