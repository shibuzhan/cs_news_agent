"""系统设置页的运行时覆盖层。

环境变量始终是可靠回退；数据库设置只对新建任务读取一次，不会中断正在执行的任务。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.services.plain_text import MAX_TARGET_CHARS, NATURAL_ARTICLE_MIN_CHARS


logger = logging.getLogger("news_agent.runtime_settings")

ModelTask = Literal["conversation", "content", "review", "illustration_planner", "evidence_selector"]
MODEL_TASKS: tuple[ModelTask, ...] = (
    "conversation", "content", "review", "illustration_planner", "evidence_selector",
)

RUNTIME_FIELD_TYPES: dict[str, type] = {
    "draft_body_min_chars": int,
    "draft_body_max_chars": int,
    "auto_review_pass_score": int,
    "wechat_open_comment": bool,
    "wechat_only_fans_can_comment": bool,
    "publication_vision_selection_enabled": bool,
    "image_generation_size": str,
    "image_generation_ratio": str,
    "image_generation_timeout_seconds": float,
    "collect_limit": int,
    "rss_feeds": str,
}

ALLOWED_IMAGE_SIZES = {"1K", "2K", "3K", "4K"}
# Agnes reference currently confirms project use of 4:3, but does not enumerate all ratios.
ALLOWED_IMAGE_RATIOS = {"4:3"}


class RuntimeSettingsError(ValueError):
    pass


def _setting_key(field: str) -> str:
    return f"runtime.{field}"


def _fernet(settings: Settings) -> Fernet:
    key = (settings.model_profile_encryption_key or "").strip()
    if not key:
        raise RuntimeSettingsError("未配置 MODEL_PROFILE_ENCRYPTION_KEY，不能保存模型密钥")
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeSettingsError("MODEL_PROFILE_ENCRYPTION_KEY 不是有效的 Fernet 密钥") from exc


def encrypt_api_key(settings: Settings, api_key: str) -> str:
    if not api_key.strip():
        raise RuntimeSettingsError("模型 API Key 不能为空")
    return _fernet(settings).encrypt(api_key.strip().encode("utf-8")).decode("utf-8")


def decrypt_api_key(settings: Settings, ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet(settings).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        logger.warning("model_profile_key_unavailable error_type=%s", type(exc).__name__)
        return None


def _parse_value(value: str, target: type) -> object:
    if target is bool:
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        raise RuntimeSettingsError("布尔设置必须是 true 或 false")
    if target is int:
        return int(value)
    if target is float:
        return float(value)
    return value


def load_runtime_settings(settings: Settings) -> Settings:
    """读取标量运行设置。数据库不可用或单项非法时安全保留环境变量值。"""
    try:
        # 延迟导入让纯加密/校验逻辑不依赖数据库驱动，也避免配置加载时建立连接。
        from app.storage.database import SessionLocal
        from app.storage.repositories import ContentRepository

        with SessionLocal() as session:
            stored = ContentRepository(session).list_app_settings()
    except Exception as exc:
        logger.debug("runtime_settings_fallback_to_env error_type=%s", type(exc).__name__)
        return settings
    updates: dict[str, object] = {}
    for field, target_type in RUNTIME_FIELD_TYPES.items():
        value = stored.get(_setting_key(field))
        if value is None:
            continue
        try:
            parsed = _parse_value(value, target_type)
            if field == "image_generation_size" and parsed not in ALLOWED_IMAGE_SIZES:
                raise RuntimeSettingsError("图片尺寸不在 Agnes 支持范围")
            if field == "image_generation_ratio" and parsed not in ALLOWED_IMAGE_RATIOS:
                raise RuntimeSettingsError("图片比例不在 Agnes 参考文档确认范围")
            updates[field] = parsed
        except (ValueError, RuntimeSettingsError):
            logger.warning("runtime_setting_ignored key=%s", field)
    # 用 getattr 读取：本函数可能拿到裁剪过的 settings（测试替身、局部构造的设置对象），
    # 而 `app_settings` 表一旦有行就会走到这里（此前表为空会提前返回，问题被掩盖）。
    current_min = getattr(settings, "draft_body_min_chars", 0)
    current_max = getattr(settings, "draft_body_max_chars", 0)
    if updates.get("draft_body_min_chars", current_min) > updates.get("draft_body_max_chars", current_max):
        logger.warning("runtime_setting_ignored invalid_body_range")
        updates.pop("draft_body_min_chars", None)
        updates.pop("draft_body_max_chars", None)
    copy_with_update = getattr(settings, "model_copy", None)
    if not callable(copy_with_update):
        return settings
    return copy_with_update(update=updates) if updates else settings


def model_profile_value(settings: Settings, task: ModelTask, field: Literal["model_name", "base_url", "api_key"]) -> str | None:
    """按任务解析模型档案；任何问题都回退到环境变量路由。"""
    try:
        from app.storage.database import SessionLocal
        from app.storage.repositories import ContentRepository

        with SessionLocal() as session:
            repository = ContentRepository(session)
            profile_id = repository.get_app_setting(f"runtime.model.{task}.profile_id")
            profile = repository.get_model_profile(profile_id) if profile_id else None
            if profile is None:
                return None
            if field == "model_name":
                return profile.model_name
            if field == "base_url":
                return profile.base_url
            return decrypt_api_key(settings, profile.encrypted_api_key)
    except Exception as exc:
        logger.debug("runtime_model_profile_fallback task=%s error_type=%s", task, type(exc).__name__)
        return None


def serialize_runtime_options(settings: Settings, stored: dict[str, str]) -> dict:
    effective = load_runtime_settings(settings)
    return {
        "draft_body_min_chars": effective.draft_body_min_chars,
        "draft_body_max_chars": effective.draft_body_max_chars,
        "auto_review_pass_score": effective.auto_review_pass_score,
        "wechat_open_comment": effective.wechat_open_comment,
        "wechat_only_fans_can_comment": effective.wechat_only_fans_can_comment,
        "publication_vision_selection_enabled": effective.publication_vision_selection_enabled,
        "image_generation_size": effective.image_generation_size,
        "image_generation_ratio": effective.image_generation_ratio,
        "image_generation_timeout_seconds": effective.image_generation_timeout_seconds,
        "collect_limit": effective.collect_limit,
        "rss_feeds": effective.rss_feeds,
        "auto_review_default": stored.get("runtime.auto_review_default", "true") == "true",
        "auto_illustration_default": stored.get("runtime.auto_illustration_default", "true") == "true",
    }


def validate_runtime_options(values: dict[str, object]) -> dict[str, str]:
    """只接受白名单字段，避免设置页成为任意环境变量写入口。"""
    accepted: dict[str, str] = {}
    for field, target in RUNTIME_FIELD_TYPES.items():
        if field not in values:
            continue
        value = values[field]
        if target is bool:
            if not isinstance(value, bool):
                raise RuntimeSettingsError(f"{field} 必须为布尔值")
            accepted[field] = "true" if value else "false"
        elif target is int:
            if not isinstance(value, int):
                raise RuntimeSettingsError(f"{field} 必须为整数")
            accepted[field] = str(value)
        elif target is float:
            if not isinstance(value, (int, float)):
                raise RuntimeSettingsError(f"{field} 必须为数字")
            accepted[field] = str(float(value))
        elif isinstance(value, str):
            accepted[field] = value.strip()
        else:
            raise RuntimeSettingsError(f"{field} 格式不正确")
    for field in ("auto_review_default", "auto_illustration_default"):
        if field in values:
            if not isinstance(values[field], bool):
                raise RuntimeSettingsError(f"{field} 必须为布尔值")
            accepted[field] = "true" if values[field] else "false"
    min_chars = int(accepted.get("draft_body_min_chars", "0") or 0)
    max_chars = int(accepted.get("draft_body_max_chars", "0") or 0)
    # 配置页填的是**目标字数**（硬区间由代码在目标上下各放宽 200 字推导），
    # 所以这里校验的是目标值：不能再靠“下限反推目标”，也不能静默抬高低到没意义的配置。
    if min_chars and max_chars and min_chars > max_chars:
        raise RuntimeSettingsError("目标最少字数不能大于目标最多字数")
    if min_chars and min_chars < NATURAL_ARTICLE_MIN_CHARS:
        raise RuntimeSettingsError(
            f"目标最少字数不得低于 {NATURAL_ARTICLE_MIN_CHARS} 字（再短就不是公众号长文）"
        )
    if max_chars and max_chars > MAX_TARGET_CHARS:
        raise RuntimeSettingsError(f"目标最多字数不得超过 {MAX_TARGET_CHARS} 字")
    if "auto_review_pass_score" in accepted and not 0 <= int(accepted["auto_review_pass_score"]) <= 100:
        raise RuntimeSettingsError("审核通过分数必须在 0 到 100 之间")
    if "image_generation_size" in accepted and accepted["image_generation_size"] not in ALLOWED_IMAGE_SIZES:
        raise RuntimeSettingsError("图片尺寸仅可选择 Agnes 文档支持的 1K、2K、3K、4K")
    if "image_generation_ratio" in accepted and accepted["image_generation_ratio"] not in ALLOWED_IMAGE_RATIOS:
        raise RuntimeSettingsError("当前 Agnes 参考文档仅确认 4:3 为项目可选比例")
    return accepted
