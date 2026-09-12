"""微信公众号官方 REST API 的受控客户端与只读权限审计命令。

凭据只从环境变量读取，命令行输出不包含 AppSecret、access token、用户标识或原始响应。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


API_BASE_URL = "https://api.weixin.qq.com"


class WechatOfficialAccountError(RuntimeError):
    """不携带令牌或原始响应的微信接口错误。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PermissionCheck:
    name: str
    method: str
    path: str
    payload: dict[str, Any] | None = None


READ_PERMISSION_CHECKS: tuple[PermissionCheck, ...] = (
    PermissionCheck("draft_count", "POST", "/cgi-bin/draft/count", {}),
    PermissionCheck("draft_list", "POST", "/cgi-bin/draft/batchget", {"offset": 0, "count": 1, "no_content": 1}),
    PermissionCheck("material_count", "GET", "/cgi-bin/material/get_materialcount"),
    PermissionCheck("material_list", "POST", "/cgi-bin/material/batchget_material", {"type": "image", "offset": 0, "count": 1}),
    PermissionCheck("auto_reply", "GET", "/cgi-bin/get_current_autoreply_info"),
    PermissionCheck("account_basic_info", "GET", "/cgi-bin/account/getaccountbasicinfo"),
    PermissionCheck("callback_ip", "GET", "/cgi-bin/getcallbackip"),
    PermissionCheck("api_domain_ip", "GET", "/cgi-bin/get_api_domain_ip"),
    PermissionCheck("publish_list", "POST", "/cgi-bin/freepublish/batchget", {"offset": 0, "count": 1, "no_content": 1}),
    PermissionCheck("user_list", "GET", "/cgi-bin/user/get"),
    PermissionCheck("tag_list", "GET", "/cgi-bin/tags/get"),
    PermissionCheck("blacklist", "POST", "/cgi-bin/tags/members/getblacklist", {"begin_openid": ""}),
    PermissionCheck("menu", "GET", "/cgi-bin/menu/get"),
    PermissionCheck("template_list", "GET", "/cgi-bin/template/get_all_private_template"),
    PermissionCheck("customer_service_accounts", "GET", "/cgi-bin/customservice/getkflist"),
)


class WechatOfficialAccountApi:
    """直接调用微信官方 API；默认不使用 Python 环境变量代理。"""

    def __init__(self, app_id: str, app_secret: str, *, timeout_seconds: float = 30.0) -> None:
        if not app_id or not app_secret:
            raise WechatOfficialAccountError("缺少 WECHAT_APP_ID 或 WECHAT_APP_SECRET")
        self.app_id = app_id
        self.app_secret = app_secret
        self.client = httpx.AsyncClient(base_url=API_BASE_URL, timeout=timeout_seconds, trust_env=False)
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    @classmethod
    def from_environment(cls) -> "WechatOfficialAccountApi":
        return cls(os.environ.get("WECHAT_APP_ID", ""), os.environ.get("WECHAT_APP_SECRET", ""))

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get_access_token(self) -> str:
        if self._access_token and self._access_token_expires_at > time.time() + 300:
            return self._access_token
        response = await self.client.get(
            "/cgi-bin/token",
            params={"grant_type": "client_credential", "appid": self.app_id, "secret": self.app_secret},
        )
        payload = self._json(response)
        self._raise_for_error(response, payload)
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not isinstance(expires_in, int):
            raise WechatOfficialAccountError("微信未返回可用访问令牌")
        self._access_token = token
        self._access_token_expires_at = time.time() + expires_in
        return token

    async def request(self, method: str, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self.get_access_token()
        response = await self.client.request(
            method,
            path,
            params={"access_token": token},
            json=payload,
        )
        response_payload = self._json(response)
        self._raise_for_error(response, response_payload)
        return response_payload

    async def list_drafts(self, *, offset: int = 0, count: int = 20) -> dict[str, Any]:
        return await self.request("POST", "/cgi-bin/draft/batchget", payload={"offset": offset, "count": count, "no_content": 1})

    async def list_permanent_images(self, *, offset: int = 0, count: int = 20) -> dict[str, Any]:
        return await self.request("POST", "/cgi-bin/material/batchget_material", payload={"type": "image", "offset": offset, "count": count})

    async def upload_permanent_image(self, content: bytes, filename: str) -> str:
        """写操作：仅在已明确授权创建草稿箱时调用。"""
        token = await self.get_access_token()
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        response = await self.client.post(
            "/cgi-bin/material/add_material",
            params={"access_token": token, "type": "image"},
            files={"media": (filename, content, mime_type)},
        )
        payload = self._json(response)
        self._raise_for_error(response, payload)
        media_id = payload.get("media_id")
        if not isinstance(media_id, str):
            raise WechatOfficialAccountError("微信未返回封面素材 ID")
        return media_id

    async def upload_inline_image(self, content: bytes, filename: str) -> str:
        """写操作：上传正文图片并返回微信可引用 URL。"""
        token = await self.get_access_token()
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        response = await self.client.post(
            "/cgi-bin/media/uploadimg",
            params={"access_token": token},
            files={"media": (filename, content, mime_type)},
        )
        payload = self._json(response)
        self._raise_for_error(response, payload)
        image_url = payload.get("url")
        if not isinstance(image_url, str):
            raise WechatOfficialAccountError("微信未返回正文图片 URL")
        return image_url

    async def create_draft(self, article: dict[str, Any]) -> str:
        """写操作：创建草稿箱条目；不得替代审核或触发发布。"""
        payload = await self.request("POST", "/cgi-bin/draft/add", payload={"articles": [article]})
        media_id = payload.get("media_id")
        if not isinstance(media_id, str):
            raise WechatOfficialAccountError("微信未返回草稿 media_id")
        return media_id

    async def update_draft(self, media_id: str, article: dict[str, Any], *, index: int = 0) -> None:
        """写操作：只更新已明确指定的一篇草稿，不创建新草稿也不发表。"""
        if not media_id:
            raise WechatOfficialAccountError("缺少待更新草稿 media_id")
        await self.request(
            "POST",
            "/cgi-bin/draft/update",
            payload={"media_id": media_id, "index": index, "articles": article},
        )

    async def audit_read_permissions(self) -> list[dict[str, str | int]]:
        results: list[dict[str, str | int]] = []
        for check in READ_PERMISSION_CHECKS:
            try:
                await self.request(check.method, check.path, payload=check.payload)
                results.append({"name": check.name, "status": "available"})
            except WechatOfficialAccountError as exc:
                status = "unauthorized" if exc.code == 48001 else "failed"
                results.append({"name": check.name, "status": status, "code": exc.code or 0})
        return results

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise WechatOfficialAccountError(f"微信接口返回非 JSON（HTTP {response.status_code}）") from exc
        if not isinstance(payload, dict):
            raise WechatOfficialAccountError("微信接口返回格式异常")
        return payload

    @staticmethod
    def _raise_for_error(response: httpx.Response, payload: dict[str, Any]) -> None:
        code = payload.get("errcode")
        if isinstance(code, int) and code != 0:
            message = payload.get("errmsg") if isinstance(payload.get("errmsg"), str) else "微信接口调用失败"
            raise WechatOfficialAccountError(f"微信接口错误 {code}: {message}", code=code)
        if response.is_error:
            raise WechatOfficialAccountError(f"微信接口 HTTP {response.status_code}")


async def _run_audit() -> int:
    api = WechatOfficialAccountApi.from_environment()
    try:
        print(json.dumps(await api.audit_read_permissions(), ensure_ascii=False, indent=2))
        return 0
    finally:
        await api.aclose()


async def _run_list(command: str) -> int:
    api = WechatOfficialAccountApi.from_environment()
    try:
        result = await (api.list_drafts() if command == "drafts" else api.list_permanent_images())
        print(json.dumps({"total_count": result.get("total_count", 0)}, ensure_ascii=False))
        return 0
    finally:
        await api.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="微信公众号官方 API 的只读审计工具")
    parser.add_argument("command", choices=("audit", "drafts", "materials"))
    args = parser.parse_args()
    try:
        return asyncio.run(_run_audit() if args.command == "audit" else _run_list(args.command))
    except WechatOfficialAccountError as exc:
        print(f"调用失败：{exc}")
        return 2
    except httpx.HTTPError as exc:
        print(f"网络请求失败：{type(exc).__name__}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
