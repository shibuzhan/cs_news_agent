"""将模型审核意见收敛为可展示、可回传的短文本。"""

from __future__ import annotations

from typing import Any


def review_feedback_text(value: object) -> str | None:
    """兼容旧模型返回的 {type, description}，绝不将对象交给前端渲染。"""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if not isinstance(value, dict):
        return None

    description = value.get("description") or value.get("message") or value.get("detail")
    if not isinstance(description, str) or not description.strip():
        return None
    category = value.get("type") or value.get("category")
    severity = value.get("severity")
    prefix = ""
    if isinstance(severity, str) and severity.strip():
        prefix = f"{severity.strip()}｜"
    if isinstance(category, str) and category.strip():
        return f"{prefix}{category.strip()}：{description.strip()}"
    return f"{prefix}{description.strip()}"


def normalize_review_feedback(values: object, limit: int = 12) -> list[str]:
    """只保留有限、去重后的可读意见，供审计、页面和自动改稿共用。"""
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        text = review_feedback_text(value)
        if text and text not in normalized:
            normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def normalized_review_report(report: object) -> dict[str, Any]:
    """在 API 边界兼容历史 JSON，保持其余审计字段不变。"""
    source = dict(report) if isinstance(report, dict) else {}
    if "issues" in source:
        source["issues"] = normalize_review_feedback(source.get("issues"))
    if "failures" in source:
        source["failures"] = normalize_review_feedback(source.get("failures"))
    if "summary" in source and not isinstance(source["summary"], str):
        source["summary"] = review_feedback_text(source["summary"]) or ""
    return source
