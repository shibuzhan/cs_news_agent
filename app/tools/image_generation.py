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
from app.services.attachments import AttachmentError, PrivateAttachmentStore, validate_image_attachment
from app.services.image_text_guard import ImageTextInspectionError, detect_visible_text
from app.storage.repositories import ContentRepository


logger = logging.getLogger("news_agent.image_generation")


class ImageGenerationError(RuntimeError):
    """不泄露供应商响应与密钥的可操作错误。"""


@dataclass(frozen=True)
class GeneratedIllustration:
    illustration_id: str
    asset_id: str
    purpose: str
    placement_after_paragraph: int


def build_safe_prompt(
    title: str,
    summary: str,
    purpose: str,
    context: str = "",
    visual_direction: str = "",
) -> str:
    role = "article cover illustration" if purpose == "cover" else "inline technology editorial illustration"
    return (
        f"Create one original, clean {role}. "
        "The source material below is semantic reference only: never copy, render, translate, or typeset it. "
        f"Topic semantics: {title[:160]}. Context semantics: {summary[:300]}. Nearby paragraph semantics: {context[:350]}. "
        f"Distinct visual direction for this image: {visual_direction[:280] or 'a single focused editorial composition'}. "
        "Show only relevant objects, technical structures, abstract data flows, or an environment background. "
        "ABSOLUTELY NO TEXT IN ANY LANGUAGE: no Chinese characters, Latin letters, numbers, glyphs, typography, captions, "
        "code, formulae, logos, watermarks, UI screenshots, labels, blank text areas, people, or recognizable product appearances. "
        "This is a text-free visual asset, not a poster or an infographic."
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
        self.settings = settings
        self.repository = repository

    async def invoke(
        self, draft_id: str, purpose: str = "inline", placement_after_paragraph: int = 1,
        context: str = "", visual_direction: str = "",
    ) -> GeneratedIllustration:
        if not self.settings.image_generation_enabled:
            raise ImageGenerationError("图片生成未启用；请先在 .env 设置 IMAGE_GENERATION_ENABLED=true")
        if not self.settings.image_generation_api_key:
            raise ImageGenerationError("图片生成服务尚未配置 API Key")
        if purpose not in {"cover", "inline"}:
            raise ImageGenerationError("插图类型仅支持封面或正文插图")
        draft = self.repository.get_draft(draft_id)
        prompt = build_safe_prompt(
            (draft.title_options_json or ["科技资讯"])[0], draft.summary_cn, purpose, context, visual_direction
        )
        content, content_type = await self._generate_without_visible_text(prompt)
        extension = ".png" if content_type == "image/png" else ".jpg"
        filename = f"generated-{draft.id[:8]}-{purpose}{extension}"
        try:
            validate_image_attachment(filename, content, content_type, self.settings)
            object_key, sha256 = await __import__("asyncio").to_thread(
                PrivateAttachmentStore(self.settings).upload, filename, content, content_type
            )
        except AttachmentError as exc:
            raise ImageGenerationError("生成图片无法保存到私有素材库") from exc
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

    async def _generate_without_visible_text(self, prompt: str) -> tuple[bytes, str]:
        """最多额外重试两次；OCR 发现文字时不允许保存或绑定素材。"""
        for attempt in range(3):
            content, content_type = await self._generate(prompt)
            try:
                detected = detect_visible_text(content)
            except ImageTextInspectionError as exc:
                raise ImageGenerationError("图片文字检测不可用，已停止保存本次图片") from exc
            if not detected:
                return content, content_type
            logger.warning("image_generation_text_rejected attempt=%s detected_count=%s", attempt + 1, len(detected))
        raise ImageGenerationError("图片生成结果包含可见文字，已重试 2 次仍不符合无文字配图要求")

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
