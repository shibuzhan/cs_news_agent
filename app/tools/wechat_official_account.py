"""应用到本地微信公众号 Skill 的受控桥接 Tool。

此 Tool 不使用 MCP/SSE。底层客户端来自
`agent_skills/wechat-official-account/scripts/wechat_official_api.py`，保持可迁移
Skill 的脚本为唯一官方 REST API 实现。
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

from app.config import Settings
from app.paths import resolve_asset
from app.services.wechat_official import (
    WechatOfficialAccountError,
    WechatRemoteDraft,
    remote_drafts_from_payload,
)

logger = logging.getLogger("news_agent.wechat_official_account")

_WECHAT_INVALID_IP_RE = re.compile(r"\binvalid ip\s+((?:\d{1,3}\.){3}\d{1,3})\b", re.IGNORECASE)

_SKILL_MODULE_NAME = "news_agent_wechat_official_account_skill"
_SKILL_SCRIPT_RELATIVE = "wechat-official-account/scripts/wechat_official_api.py"


def _skill_script_path() -> Path:
    """定位本地 Skill 脚本；容器内可能从 site-packages 导入，因此按候选资源根解析。"""
    script = resolve_asset(_SKILL_SCRIPT_RELATIVE)
    if script is None:
        raise RuntimeError("微信公众号本地 Skill 脚本不存在")
    return script


def _load_skill_module() -> ModuleType:
    """按绝对路径加载 Skill 脚本，避免连字符目录成为 Python 包名限制。"""
    loaded = sys.modules.get(_SKILL_MODULE_NAME)
    if isinstance(loaded, ModuleType):
        return loaded
    spec = importlib.util.spec_from_file_location(_SKILL_MODULE_NAME, _skill_script_path())
    if spec is None or spec.loader is None:
        raise RuntimeError("微信公众号本地 Skill 脚本不可加载")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SKILL_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class WechatOfficialAccountTool:
    """封装官方接口调用、脱敏错误和单次投递的内存令牌缓存。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._api: Any | None = None
        self._skill_error_type: type[Exception] | None = None

    async def __aenter__(self) -> "WechatOfficialAccountTool":
        if not self.settings.wechat_app_id or not self.settings.wechat_app_secret:
            raise WechatOfficialAccountError(
                "未配置 WECHAT_APP_ID 或 WECHAT_APP_SECRET，无法调用微信公众号官方接口。",
                category="configuration",
            )
        try:
            module = _load_skill_module()
            api_class = module.WechatOfficialAccountApi
            self._skill_error_type = module.WechatOfficialAccountError
            self._api = api_class(
                self.settings.wechat_app_id or "",
                self.settings.wechat_app_secret or "",
                timeout_seconds=self.settings.wechat_api_timeout_seconds,
            )
        except Exception as exc:  # 配置错误同样不得泄露原始环境变量。
            raise self._safe_error("initialize", exc) from exc
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self._api is not None:
            await self._api.aclose()
        self._api = None

    async def upload_cover(self, content: bytes, filename: str) -> str:
        logger.info("wechat_official_cover_upload_started filename=%s size_bytes=%s", filename, len(content))
        return await self._call("upload_cover", self._require_api().upload_permanent_image(content, filename))

    async def upload_inline_image(self, content: bytes, filename: str) -> str:
        # 不做 1 MB 本地预检：由官方 uploadimg 接口按当前规则裁决。
        logger.info("wechat_official_inline_upload_started filename=%s size_bytes=%s", filename, len(content))
        return await self._call("upload_inline_image", self._require_api().upload_inline_image(content, filename))

    def _article_payload(
        self, *, title: str, digest: str, content_html: str, source_url: str, cover_media_id: str
    ) -> dict[str, Any]:
        """草稿文章字段。

        `need_open_comment` / `only_fans_can_comment` 必须显式给出：微信在这两个字段缺省时
        按 **0（留言关闭）** 处理——这就是此前所有草稿留言功能都是关闭状态的原因。
        """
        return {
            "title": title,
            "author": "资讯运营 Agent",
            "digest": digest,
            "content": content_html,
            "content_source_url": source_url,
            "thumb_media_id": cover_media_id,
            "show_cover_pic": 1,
            "need_open_comment": 1 if self.settings.wechat_open_comment else 0,
            "only_fans_can_comment": 1 if self.settings.wechat_only_fans_can_comment else 0,
        }

    async def create_draft(
        self,
        *,
        title: str,
        digest: str,
        content_html: str,
        source_url: str,
        cover_media_id: str,
    ) -> str:
        article = self._article_payload(
            title=title, digest=digest, content_html=content_html,
            source_url=source_url, cover_media_id=cover_media_id,
        )
        return await self._call("create_draft", self._require_api().create_draft(article))

    async def update_draft(
        self,
        *,
        media_id: str,
        title: str,
        digest: str,
        content_html: str,
        source_url: str,
        cover_media_id: str,
    ) -> None:
        """只覆盖指定远端草稿，复用已上传的封面和正文图片，不创建新草稿。"""
        article = self._article_payload(
            title=title, digest=digest, content_html=content_html,
            source_url=source_url, cover_media_id=cover_media_id,
        )
        await self._call("update_draft", self._require_api().update_draft(media_id, article))

    async def list_remote_drafts(self, *, offset: int = 0, count: int = 20) -> tuple[int, list[WechatRemoteDraft]]:
        payload = await self._call("list_drafts", self._require_api().list_drafts(offset=offset, count=count))
        return remote_drafts_from_payload(payload)

    def _require_api(self) -> Any:
        if self._api is None:
            raise WechatOfficialAccountError("微信公众号本地 Tool 尚未初始化", category="tool_state")
        return self._api

    async def _call(self, operation: str, awaitable: Any) -> Any:
        try:
            return await awaitable
        except Exception as exc:
            raise self._safe_error(operation, exc) from exc

    def _safe_error(self, operation: str, exc: Exception) -> WechatOfficialAccountError:
        code = getattr(exc, "code", None)
        provider_code = str(code) if isinstance(code, int) else None
        logger.warning(
            "wechat_official_request_failed operation=%s error_type=%s provider_code=%s",
            operation,
            type(exc).__name__,
            provider_code or "none",
        )
        if code == 40164:
            match = _WECHAT_INVALID_IP_RE.search(str(exc))
            current_ip = match.group(1) if match else None
            ip_hint = f"微信识别的当前出口 IP：{current_ip}。" if current_ip else "微信未返回可识别的当前出口 IP。"
            return WechatOfficialAccountError(
                "公众号出口 IP 未加入白名单（微信接口 40164）。"
                + ip_hint
                + "请在公众号后台“设置与开发 → 开发接口管理 → 基本配置 → IP 白名单”添加该 IP 后重试。",
                category="wechat_ip_whitelist",
                provider_code="40164",
            )
        if code == 48001:
            return WechatOfficialAccountError(
                "当前公众号未获该微信接口授权（错误码 48001），请检查账号类型与接口权限。",
                category="wechat_permission",
                provider_code="48001",
            )
        if code in {40001, 40013, 40101, 42001}:
            return WechatOfficialAccountError(
                "公众号凭据不可用或已失效，请检查 AppID、AppSecret 与账号授权后重试。",
                category="credential_or_permission",
                provider_code=provider_code,
            )
        if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
            return WechatOfficialAccountError(
                "微信公众号官方接口响应超时，请检查网络连接后重试。",
                category="network_timeout",
            )
        if isinstance(exc, httpx.HTTPError):
            return WechatOfficialAccountError(
                "微信公众号官方接口网络连接失败，请检查出站网络后重试。",
                category="network",
            )
        if self._skill_error_type is not None and isinstance(exc, self._skill_error_type):
            return WechatOfficialAccountError(
                "微信公众号官方接口拒绝本次请求，请检查素材格式、大小和接口权限后重试。",
                category="official_api_error",
                provider_code=provider_code,
            )
        return WechatOfficialAccountError(
            "微信公众号本地 Tool 暂时不可用，请查看应用安全日志后重试。",
            category="tool_error",
        )
