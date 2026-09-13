"""长期偏好：Agent 可以持久修改，之后每篇文章都按它排版。

分两类存放（各取所长）：
- **结构化开关与指针**（`cover_in_body`、结尾图素材 id、微信 URL）→ `app_settings` 表：
  这些要由**代码确定性执行**，不能靠模型理解散文；
- **长文风格说明** → **仓库文件** `preferences/style.md`（挂载进容器）：
  表达力强、Git 可直接评审，读取由应用代码完成（与读 `agent_skills/*/SKILL.md` 同一机制），
  因此不需要给对话 Agent 文件系统权限。

读取顺序：`preferences/style.md` → 数据库短条目 `style_notes`（两者都为空则不注入）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.storage.repositories import ContentRepository

logger = logging.getLogger(__name__)

KEY_COVER_IN_BODY = "publication.cover_in_body"
KEY_FOOTER_IMAGE_URL = "publication.footer_image_url"
KEY_FOOTER_IMAGE_ASSET = "publication.footer_image_asset_id"
KEY_FOOTER_TEXT_ENABLED = "publication.footer_text_enabled"
# 通用长期偏好：任何“文字层面”的要求都能存进来，并被生成/改稿/审核三处提示词读取。
# 这样新增偏好不需要改代码，除非它需要流程里不存在的新能力（那种情况会明确告诉用户）。
KEY_STYLE_NOTES = "publication.style_notes"

STYLE_GUIDE_FILENAME = "style.md"
PREFERENCES_DIR_ENV = "NEWS_AGENT_PREFERENCES_DIR"
_TEMPLATE_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

_TRUE = {"1", "true", "yes", "on", "是", "开"}
MAX_STYLE_NOTES = 20
MAX_STYLE_NOTE_CHARS = 300


def preferences_dir() -> Path:
    """偏好目录：容器内为挂载点 /app/preferences，宿主机为仓库 preferences/。"""
    override = os.environ.get(PREFERENCES_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "preferences"


def style_guide_path() -> Path:
    return preferences_dir() / STYLE_GUIDE_FILENAME


def load_style_guide() -> str:
    """读取长文风格说明；文件缺失、为空或只剩模板注释时返回空串（走回退）。"""
    try:
        raw = style_guide_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    return _TEMPLATE_COMMENT.sub("", raw).strip()


def save_style_guide(markdown: str) -> int:
    """写入长文风格说明（原子替换），返回写入的字节数。"""
    path = style_guide_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = markdown if markdown.endswith("\n") else f"{markdown}\n"
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return len(payload.encode("utf-8"))


def load_style_notes(repository: ContentRepository) -> tuple[str, ...]:
    loader = getattr(repository, "get_app_setting", None)
    raw = loader(KEY_STYLE_NOTES) if callable(loader) else None
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(parsed, list):
        return tuple(str(item).strip() for item in parsed if str(item).strip())
    return ()


def save_style_notes(repository: ContentRepository, notes: list[str], *, updated_by: str = "agent") -> tuple[str, ...]:
    cleaned: list[str] = []
    for note in notes:
        text = str(note).strip()[:MAX_STYLE_NOTE_CHARS]
        if text and text not in cleaned:
            cleaned.append(text)
    cleaned = cleaned[:MAX_STYLE_NOTES]
    repository.set_app_setting(KEY_STYLE_NOTES, json.dumps(cleaned, ensure_ascii=False), updated_by=updated_by)
    return tuple(cleaned)


def add_style_note(repository: ContentRepository, note: str) -> tuple[str, ...]:
    return save_style_notes(repository, [*load_style_notes(repository), note])


def remove_style_note(repository: ContentRepository, note: str) -> tuple[str, ...]:
    target = str(note).strip()
    return save_style_notes(repository, [item for item in load_style_notes(repository) if item != target])


def preference_rules_block(repository: ContentRepository) -> str:
    """把长期偏好渲染成可直接拼进提示词的规则块；都没有时返回空串。

    来源两处，顺序即优先级：
    1. **仓库文件** `preferences/style.md`（长文风格说明，Git 可评审，Agent 可用工具改）；
    2. 数据库短条目 `style_notes`（快速记录）。

    生成、改稿、审核三处共用，因此新增偏好不必改代码——只要它是文字层面的要求。
    """
    guide = load_style_guide()
    notes = load_style_notes(repository)
    if not guide and not notes:
        return ""
    sections: list[str] = ["\n运营者的长期偏好（必须遵守，与以下规则冲突时以本节为准）："]
    if guide:
        sections.append(guide)
    if notes:
        sections.append("\n".join(f"- {note}" for note in notes))
    return "\n".join(sections) + "\n"


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
