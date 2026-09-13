"""长期排版偏好：Agent 可以持久修改，之后每篇文章都按它排版。

三项偏好：
- `cover_in_body`：封面图同时作为**正文首图**（默认开，用户要求）；
- `footer_image_url` / `footer_image_asset_id`：**固定结尾图**；
- `footer_text_enabled`：是否保留原来的**文字尾注**（设置了结尾图后默认关闭，即“以图取代文字”）。

值放在 `app_settings` 表里，因此容器重建也不会丢。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.storage.repositories import ContentRepository

logger = logging.getLogger(__name__)

KEY_COVER_IN_BODY = "publication.cover_in_body"
KEY_FOOTER_IMAGE_URL = "publication.footer_image_url"
KEY_FOOTER_IMAGE_ASSET = "publication.footer_image_asset_id"
KEY_FOOTER_TEXT_ENABLED = "publication.footer_text_enabled"

_TRUE = {"1", "true", "yes", "on", "是", "开"}


@dataclass(frozen=True)
class PublicationPreferences:
    cover_in_body: bool = True
    footer_image_url: str | None = None
    footer_image_asset_id: str | None = None
    footer_text_enabled: bool = True

    @property
    def footer_image_configured(self) -> bool:
        return bool(self.footer_image_url)


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in _TRUE


def load_publication_preferences(repository: ContentRepository) -> PublicationPreferences:
    # 兼容只实现了部分接口的仓储（测试替身/裁剪过的实现）：读不到设置就用默认值。
    loader = getattr(repository, "list_app_settings", None)
    values = loader() if callable(loader) else {}
    footer_url = (values.get(KEY_FOOTER_IMAGE_URL) or "").strip() or None
    # 配了结尾图就默认不再渲染文字尾注——“以图取代原来的文字”。
    default_text_footer = not footer_url
    return PublicationPreferences(
        cover_in_body=_as_bool(values.get(KEY_COVER_IN_BODY), True),
        footer_image_url=footer_url,
        footer_image_asset_id=(values.get(KEY_FOOTER_IMAGE_ASSET) or "").strip() or None,
        footer_text_enabled=_as_bool(values.get(KEY_FOOTER_TEXT_ENABLED), default_text_footer),
    )


def save_publication_preferences(
    repository: ContentRepository,
    *,
    cover_in_body: bool | None = None,
    footer_image_url: str | None = None,
    footer_image_asset_id: str | None = None,
    footer_text_enabled: bool | None = None,
    updated_by: str = "agent",
) -> PublicationPreferences:
    """写入偏好；只更新显式给出的项。`footer_image_url=""` 表示清除结尾图。"""
    if cover_in_body is not None:
        repository.set_app_setting(KEY_COVER_IN_BODY, "true" if cover_in_body else "false", updated_by=updated_by)
    if footer_image_url is not None:
        repository.set_app_setting(KEY_FOOTER_IMAGE_URL, footer_image_url, updated_by=updated_by)
        # 新设或清除结尾图时，文字尾注默认跟随（设图则关、清图则开），除非调用方另行指定。
        if footer_text_enabled is None:
            repository.set_app_setting(
                KEY_FOOTER_TEXT_ENABLED, "false" if footer_image_url else "true", updated_by=updated_by
            )
    if footer_image_asset_id is not None:
        repository.set_app_setting(KEY_FOOTER_IMAGE_ASSET, footer_image_asset_id, updated_by=updated_by)
    if footer_text_enabled is not None:
        repository.set_app_setting(KEY_FOOTER_TEXT_ENABLED, "true" if footer_text_enabled else "false", updated_by=updated_by)
    preferences = load_publication_preferences(repository)
    logger.info(
        "publication_preferences_saved cover_in_body=%s footer_image=%s footer_text=%s",
        preferences.cover_in_body, bool(preferences.footer_image_url), preferences.footer_text_enabled,
    )
    return preferences
