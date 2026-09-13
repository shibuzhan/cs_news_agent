"""受控图片生成 Tool：只为已有草稿生成通用插图并私有保存。"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.services.attachments import (
    AttachmentError,
    AttachmentValidationError,
    PrivateAttachmentStore,
    validate_image_attachment,
)
from app.services.image_brief import FALLBACK_SCENE, load_brief, pick_object, pick_style
from app.services.image_text_guard import ImageTextInspectionError, cjk_text, detect_visible_text
from app.services.runtime_settings import load_runtime_settings
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.image_generation")


class ImageGenerationError(RuntimeError):
    """不泄露供应商响应与密钥的可操作错误。"""


def _asset_save_reason(exc: AttachmentError, size_bytes: int) -> str:
    """把素材库写入失败翻译成可操作原因；两类问题（体积超限 / 存储不可用）必须能区分。"""
    if isinstance(exc, AttachmentValidationError):
        return f"生成的图片体积（{size_bytes / (1024 * 1024):.1f} MB）超过本地保存上限，已停止保存"
    return "生成图片无法保存到私有素材库：私有素材存储暂时不可用"


@dataclass(frozen=True)
class GeneratedIllustration:
    illustration_id: str
    asset_id: str
    purpose: str
    placement_after_paragraph: int


def topic_keywords(title: str, summary: str, context: str = "", limit: int = 400) -> str:
    """主题关键词只作语气参考；实测把“不要渲染这些文字”写进提示词反而会诱发伪文字。"""
    parts: list[str] = []
    for value in (title, summary, context):
        text = " ".join(str(value or "").split())
        if text and text not in parts:
            parts.append(text)
    return " | ".join(parts)[:limit]


# 只保留两类约束：**正向允许清单**（画面只包含场景中列出的实物）与唯一必要的负向约束（中文等 CJK 字形 +
# 品牌标识 + 人物，分别对应本地 OCR 质量门与配图合规边界）。风格类禁令（全息面板、电路板、大脑、霓虹、
# 水彩、箭头、3D 渲染等）已全部删除：实测把它们逐条写进禁则后，三张样图反而全部画出了这些元素——负向
# 列举会被图像模型当成内容提示（“别想大象”效应）。拉丁字母与数字不禁止：实物本身常带刻度与印刷。
_FRAME_CONSTRAINTS = (
    "Show only the objects described above, in a quiet uncluttered frame; every surface stays "
    "clean and blank. No Chinese characters or other CJK glyphs anywhere in the image; "
    "no logos, watermarks, people or hands."
)

# 按用途区分构图：封面要留标题空间，正文插图贴近细节。风格与实物来自来源 brief。
_COVER_COMPOSITION = (
    "Composition: one clear subject placed off-centre inside a calm background, generous empty "
    "space on one side for a headline"
)
_INLINE_COMPOSITION = (
    "Composition: a close, intimate view of the subject with a quiet uncluttered background"
)


def build_safe_prompt(
    title: str,
    summary: str,
    purpose: str,
    context: str = "",
    visual_direction: str = "",
    *,
    source_kind: str | None = None,
    placement_after_paragraph: int = 1,
    ratio: str = "4:3",
    style: str = "",
    subject: str = "",
    draft_id: str | None = None,
) -> str:
    """来源 Skill 给出风格池与实物池，服务端决定风格、主体、构图与约束。

    风格与主体优先用调用方给出的文本（规划模型在池内选的编号对应项，或任务落库的值）；
    否则按草稿 ID 在池内哈希轮换（风格按文章、主体按位置），因此同一来源的不同文章不会重复同一张图。
    """
    brief = load_brief(source_kind)
    chosen_subject = (subject or "").strip()
    chosen_style = (style or "").strip()
    if brief is not None:
        if not chosen_style:
            chosen_style = pick_style(brief, draft_id, purpose, placement_after_paragraph)
        if not chosen_subject:
            chosen_subject = pick_object(brief, draft_id, purpose, placement_after_paragraph)
        parts = [chosen_style, f"Subject: {chosen_subject}." if chosen_subject else ""]
    else:
        # 来源 brief 缺失时退回调用方给出的方向，最后退回中性兜底场景（自带风格）。
        parts = [(visual_direction or "").strip() or FALLBACK_SCENE]
    composition = _COVER_COMPOSITION if purpose == "cover" else _INLINE_COMPOSITION
    scene = " ".join(part for part in parts if part).rstrip().rstrip(".")
    return (
        f"{scene}. "
        f"{composition}, {ratio} framing; the image must still read clearly when scaled down. "
        f"{_FRAME_CONSTRAINTS} "
        "Background context for tone only: "
        f"{topic_keywords(title, summary, context)}."
    )


def decode_nonempty_base64_image(value: object) -> bytes | None:
    """供应商有时同时返回空 b64_json 和可用 URL；空值必须回退 URL。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    return content or None


class ImageGenerationTool:
    """Agnes 兼容图片接口的最小适配器；仅接受项目配置的固定地址。"""

    def __init__(self, settings: Settings, repository: ContentRepository):
        self.settings = load_runtime_settings(settings)
        self.repository = repository

    async def invoke(
        self, draft_id: str, purpose: str = "inline", placement_after_paragraph: int = 1,
        context: str = "", visual_direction: str = "", subject: str = "", style: str = "",
    ) -> GeneratedIllustration:
        if not self.settings.image_generation_enabled:
            raise ImageGenerationError("图片生成未启用；请先在 .env 设置 IMAGE_GENERATION_ENABLED=true")
        if not self.settings.image_generation_api_key:
            raise ImageGenerationError("图片生成服务尚未配置 API Key")
        if purpose not in {"cover", "inline"}:
            raise ImageGenerationError("插图类型仅支持封面或正文插图")
        draft = self.repository.get_draft(draft_id)
        prompt = build_safe_prompt(
            (draft.title_options_json or ["科技资讯"])[0],
            draft.summary_cn,
            purpose,
            context,
            visual_direction,
            source_kind=getattr(draft.source_item, "source_kind", None),
            placement_after_paragraph=placement_after_paragraph,
            ratio=self.settings.image_generation_ratio,
            style=style,
            subject=subject,
            draft_id=draft.id,
        )
        content, content_type = await self._generate_without_cjk_text(prompt)
        extension = ".png" if content_type == "image/png" else ".jpg"
        filename = f"generated-{draft.id[:8]}-{purpose}{extension}"
        try:
            validate_image_attachment(
                filename, content, content_type, self.settings, self.settings.generated_image_max_bytes
            )
            object_key, sha256 = await __import__("asyncio").to_thread(
                PrivateAttachmentStore(self.settings).upload,
                filename, content, content_type, self.settings.generated_image_max_bytes,
            )
        except AttachmentError as exc:
            # 失败原因必须落到日志与失败原因里：超本地大小上限与素材库不可用是两类问题。
            logger.warning(
                "image_asset_save_failed draft_id=%s purpose=%s size_bytes=%s error_type=%s",
                draft.id, purpose, len(content), type(exc).__name__,
            )
            raise ImageGenerationError(_asset_save_reason(exc, len(content))) from exc
        asset = self.repository.create_publication_asset(
            filename, content_type, object_key, len(content), sha256
        )
        illustration = self.repository.create_draft_illustration(
            draft_id, asset.id, purpose, placement_after_paragraph,
            prompt=prompt, provider=self.settings.image_generation_provider,
            model=self.settings.image_generation_model,
        )
        logger.info(
            "draft_image_generated draft_id=%s illustration_id=%s purpose=%s",
            draft_id, illustration.id, purpose,
        )
        return GeneratedIllustration(
            illustration.id, asset.id, purpose, placement_after_paragraph
        )

    async def _generate_without_cjk_text(self, prompt: str) -> tuple[bytes, str]:
        """最多额外重试两次；OCR 检出的中文/CJK 字形视为缺陷，纯拉丁字母与数字放行。"""
        for attempt in range(3):
            content, content_type = await self._generate(prompt)
            try:
                detected = detect_visible_text(content)
            except ImageTextInspectionError as exc:
                raise ImageGenerationError("图片文字检测不可用，已停止保存本次图片") from exc
            cjk = cjk_text(detected)
            if not cjk:
                return content, content_type
            logger.warning("image_generation_cjk_rejected attempt=%s detected_count=%s", attempt + 1, len(cjk))
        raise ImageGenerationError("图片生成结果包含中文文字，已重试 2 次仍不符合无中文配图要求")

    async def _generate(self, prompt: str) -> tuple[bytes, str]:
        base_url = self.settings.image_generation_base_url.rstrip("/")
        endpoint = f"{base_url}/v1/images/generations"
        headers = {"Authorization": f"Bearer {self.settings.image_generation_api_key}"}
        payload = {
            "model": self.settings.image_generation_model,
            "prompt": prompt,
            "size": self.settings.image_generation_size,
            "ratio": self.settings.image_generation_ratio,
            "n": 1,
            "extra_body": {"response_format": "b64_json"},
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.image_generation_timeout_seconds) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json().get("data", [])
                first = data[0] if isinstance(data, list) and data else {}
                content = decode_nonempty_base64_image(first.get("b64_json")) if isinstance(first, dict) else None
                if content is not None:
                    return content, "image/png"
                image_url = first.get("url") if isinstance(first, dict) else None
                if not isinstance(image_url, str) or urlparse(image_url).scheme != "https":
                    raise ImageGenerationError("图片服务返回格式不受支持")
                image_response = await client.get(image_url)
                image_response.raise_for_status()
                content_type = image_response.headers.get("content-type", "").split(";", 1)[0]
                if content_type not in {"image/jpeg", "image/png"}:
                    raise ImageGenerationError("图片服务返回了不支持的图片格式")
                return image_response.content, content_type
        except ImageGenerationError:
            raise
        except (httpx.HTTPError, ValueError, base64.binascii.Error) as exc:
            logger.warning("image_generation_failed error_type=%s", type(exc).__name__)
            raise ImageGenerationError("图片生成服务暂时不可用，请检查配置、额度和网络后重试") from exc
