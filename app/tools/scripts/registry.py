from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


class RegisteredScriptError(ValueError):
    pass


@dataclass(frozen=True)
class RegisteredScript:
    script_id: str
    description: str
    transform: Callable[[str], str]


def _outline(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "# 文件大纲\n\n" + "\n".join(f"- {line[:160]}" for line in lines[:30])


def _csv_brief(text: str) -> str:
    rows = [line for line in text.splitlines() if line.strip()]
    return "# CSV 选题简报\n\n" + f"- 有效行数：{max(0, len(rows) - 1)}\n- 表头：{rows[0][:300] if rows else '无'}"


SCRIPTS: dict[str, RegisteredScript] = {
    "extract_text_outline": RegisteredScript("extract_text_outline", "从已授权文本提取大纲", _outline),
    "csv_to_topic_brief": RegisteredScript("csv_to_topic_brief", "将 CSV 归纳为选题简报", _csv_brief),
}


def run_registered_script(script_id: str, text: str) -> str:
    script = SCRIPTS.get(script_id)
    if script is None:
        raise RegisteredScriptError("脚本未登记，不能执行")
    return script.transform(text)
