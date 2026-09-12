"""生成图片的本地文字质量门：检测到任何可见文字时拒绝入库。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


class ImageTextInspectionError(RuntimeError):
    """OCR 未能可靠完成时采用失败关闭，避免不合格素材进入草稿。"""


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
