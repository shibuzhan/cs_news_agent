from __future__ import annotations


class ChatAgent:
    """普通对话不调用 LLM；只识别明确的附件提取指令。"""

    @staticmethod
    def requests_attachment_extraction(content: str, attachment_id: str | None) -> bool:
        normalized = "".join(content.casefold().split())
        return bool(
            attachment_id
            and "提取" in normalized
            and "附件" in normalized
            and ("生成" in normalized or "创建" in normalized)
            and "草稿" in normalized
        )

    @staticmethod
    def regular_reply() -> str:
        return "消息与附件已保存。需要处理附件时，请明确说“提取附件并生成待审核草稿”。"
