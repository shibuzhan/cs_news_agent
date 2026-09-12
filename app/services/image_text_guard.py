"""生成图片的本地文字质量门：检出中文/CJK 字形时拒绝入库。

口径（2026-09-12 按用户确认收窄）：拉丁字母与数字属于实物本身的正常细节（尺子刻度、键盘键帽、
包装印刷），可以接受；中文与其他 CJK 字形在图像模型里会渲染成乱码方块，必须拒绝。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any


class ImageTextInspectionError(RuntimeError):
    """OCR 未能可靠完成时采用失败关闭，避免不合格素材进入草稿。"""


# CJK 统一表意文字（含扩展 A 与兼容区）、CJK 标点、假名、全角/半角形式。
_CJK_PATTERN = re.compile(
    "[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)


def contains_cjk(text: str) -> bool:
    """该串是否含中文或其他 CJK 字形。"""
    return bool(_CJK_PATTERN.search(text))


def cjk_text(lines: list[str]) -> list[str]:
    """只挑出含 CJK 字形的检出结果；纯拉丁字母或数字按既定口径放行。"""
    return [line for line in lines if contains_cjk(line)]


@lru_cache(maxsize=1)
def _ocr_engine() -> Any:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise ImageTextInspectionError("图片文字检测组件不可用") from exc
    return RapidOCR()


def detect_visible_text(content: bytes, minimum_confidence: float = 0.6) -> list[str]:
    """返回可信 OCR 文本；任意非空结果都表示图片不适合作为无文字配图。"""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ImageTextInspectionError("图片文字检测组件不可用") from exc
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ImageTextInspectionError("图片文字检测无法读取生成结果")
    try:
        result = _ocr_engine()(image)
    except Exception as exc:
        raise ImageTextInspectionError("图片文字检测未完成") from exc
    lines = result[0] if isinstance(result, tuple) else result
    if not isinstance(lines, list):
        return []
    detected: list[str] = []
    for line in lines:
        if not isinstance(line, (list, tuple)) or len(line) < 2:
            continue
        text_info = line[1]
        if not isinstance(text_info, (list, tuple)) or len(text_info) < 2:
            continue
        text, confidence = text_info[0], text_info[1]
        if isinstance(text, str) and text.strip() and isinstance(confidence, (float, int)) and confidence >= minimum_confidence:
            detected.append(text.strip())
    return detected
