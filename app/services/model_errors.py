"""将模型 SDK 异常转换为可审计、无敏感信息的运营提示。

两条边界必须同时成立：
1. 写回界面、数据库和审计的原因只能是固定分类短句，绝不包含供应商原始响应、请求 ID 或密钥；
2. 分类只使用异常类型与结构化的 `status_code` / `code` / `param` 字段，原始文本仅用于判断原因。
"""

from __future__ import annotations

from typing import Any


# 只应留在运行日志里的长噪声在已落库文本中的特征；命中即判定为需要脱敏的历史脏数据。
_RAW_TEXT_MARKERS = (
    "Error code:",
    "Traceback (most recent call last)",
    "openai.",
    "litellm.",
    "{'error'",
    '{"error"',
    # 历史 Pydantic 校验转储同样是多行长噪声。
    "validation error for",
    "pydantic.dev",
    "input_value=",
    "[type=",
)

# SDK 异常类名特征，用于兼容测试替身与不同 SDK 版本的类名差异。
_PROVIDER_ERROR_NAME_HINTS = (
    "APIStatusError",
    "APIConnectionError",
    "APITimeoutError",
    "OpenAIError",
    "APIError",
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "RateLimitError",
    "InternalServerError",
)

STRUCTURED_OUTPUT_REASON = (
    "内容模型不接受强制工具调用（思考模式限制）：请将该任务的结构化输出模式改为 json"
)
QUOTA_REASON = "内容模型服务额度不足"
RATE_LIMIT_REASON = "内容模型服务限流或额度不足"
CREDENTIAL_REASON = "内容模型凭据或权限不可用"
MODEL_REASON = "内容模型名称不可用"
REJECTED_REASON = "内容模型拒绝了本次请求（请求参数不被支持）"
SERVER_REASON = "内容模型服务返回异常状态"
CONNECTION_REASON = "内容模型服务连接失败"
UNAVAILABLE_REASON = "内容模型暂时不可用"
STRUCTURE_REASON = "内容模型输出不符合草稿结构，请重试或检查模型配置"
CLEANED_FALLBACK_REASON = "内容模型调用失败，请查看运行日志"
# 来源正文缺失时不生成草稿：仅在 Trending 简介上写长文只会得到空话。
README_UNAVAILABLE_REASON = (
    "未获取到项目 README（GitHub 接口限流或不可用），已停止生成：请稍后重试，或配置 GITHUB_TOKEN 提高配额"
)
GITHUB_TOKEN_HINT = "配置 GITHUB_TOKEN"
SAFE_TEXT_MAX_CHARS = 300


def model_failure_message(exc: Exception, operation: str, timeout_seconds: float) -> str:
    """审核与改稿链路使用的简短原因；不返回供应商原始响应、URL、请求头或密钥。"""
    error_type = type(exc).__name__
    if "Timeout" in error_type:
        return f"{operation}模型请求超时（{timeout_seconds:g} 秒）"
    if error_type in {"AuthenticationError", "PermissionDeniedError"}:
        return f"{operation}模型凭据或权限不可用"
    if error_type == "NotFoundError":
        return f"{operation}模型名称不可用"
    if error_type == "RateLimitError":
        return f"{operation}模型服务限流或额度不足"
    if "Connection" in error_type or "APIConnection" in error_type:
        return f"{operation}模型服务连接失败"
    if "StatusError" in error_type:
        return f"{operation}模型服务返回异常状态"
    return f"{operation}模型暂时不可用"


def _error_payload(exc: Exception) -> dict[str, Any]:
    """提取结构化错误字段；不存在时返回空字典，不回退到原始文本解析。"""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        return error if isinstance(error, dict) else body
    error = getattr(exc, "error", None)
    return error if isinstance(error, dict) else {}


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_provider_error(exc: Exception) -> bool:
    """判断是否为模型 SDK／网关错误，不依赖具体 SDK 版本。

    必须匹配整条继承链上的类名：langchain-openai 会把 SDK 异常再包一层
    （`OpenAITimeoutError(openai.APITimeoutError)`、`OpenAIConnectionError(openai.APIConnectionError)`），
    只看最外层类名会漏判，导致原始英文报错直接写进界面。
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if _status_code(exc) is not None:
        return True
    if isinstance(getattr(exc, "body", None), (dict, str)):
        return True
    names = " ".join(cls.__name__ for cls in type(exc).__mro__)
    return any(hint in names for hint in _PROVIDER_ERROR_NAME_HINTS)


def is_structured_output_rejection(exc: Exception) -> bool:
    """思考模式模型拒绝 `tool_choice=required` 或具体工具对象。"""
    payload = _error_payload(exc)
    parts = (payload.get("param"), payload.get("code"), payload.get("message"))
    haystack = " ".join(str(part) for part in parts if part).casefold()
    return "tool_choice" in haystack or "thinking mode" in haystack


def generation_failure_message(exc: Exception, timeout_seconds: float | None = None) -> str:
    """生成链路的可操作失败原因；只输出分类短句。"""
    error_type = type(exc).__name__
    if isinstance(exc, TimeoutError) or "Timeout" in error_type:
        return f"内容模型请求超时（{timeout_seconds:g} 秒）" if timeout_seconds else "内容模型请求超时"
    if isinstance(exc, ConnectionError) or "Connection" in error_type:
        return CONNECTION_REASON
    status = _status_code(exc)
    if status == 400:
        return STRUCTURED_OUTPUT_REASON if is_structured_output_rejection(exc) else REJECTED_REASON
    if status == 402:
        return QUOTA_REASON
    if status in {401, 403}:
        return CREDENTIAL_REASON
    if status == 404:
        return MODEL_REASON
    if status == 408:
        return "内容模型请求超时"
    if status == 429:
        return RATE_LIMIT_REASON
    if status is not None and status >= 500:
        return SERVER_REASON
    if "Authentication" in error_type or "Permission" in error_type:
        return CREDENTIAL_REASON
    if "NotFound" in error_type:
        return MODEL_REASON
    if "RateLimit" in error_type:
        return RATE_LIMIT_REASON
    if "Status" in error_type or "ServerError" in error_type:
        return SERVER_REASON
    return UNAVAILABLE_REASON


def _reason_from_raw_text(text: str) -> str:
    """从已落库的原始文本中判定原因；只做关键字分类，不回显该文本。"""
    haystack = text.casefold()
    if "validation error for" in haystack or "pydantic.dev" in haystack or "input_value=" in haystack:
        return STRUCTURE_REASON
    if "不符合草稿结构" in text or "不符合结构" in text:
        return STRUCTURE_REASON
    if "tool_choice" in haystack or "thinking mode" in haystack:
        return STRUCTURED_OUTPUT_REASON
    if "insufficient balance" in haystack or "insufficient_quota" in haystack or "error code: 402" in haystack:
        return QUOTA_REASON
    if "error code: 401" in haystack or "error code: 403" in haystack:
        return CREDENTIAL_REASON
    if "error code: 404" in haystack:
        return MODEL_REASON
    if "error code: 429" in haystack or "rate limit" in haystack:
        return RATE_LIMIT_REASON
    if "error code: 5" in haystack or "internal server error" in haystack:
        return SERVER_REASON
    if "timeout" in haystack or "timed out" in haystack:
        return "内容模型请求超时"
    if "connection" in haystack:
        return CONNECTION_REASON
    return CLEANED_FALLBACK_REASON


def sanitize_failure_text(value: object, limit: int = SAFE_TEXT_MAX_CHARS) -> str:
    """清理失败文本：截掉供应商原始响应片段并保留可读前缀；干净文本原样返回。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    marker_positions = [text.find(marker) for marker in _RAW_TEXT_MARKERS]
    index = min((position for position in marker_positions if position >= 0), default=-1)
    if index < 0:
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    prefix = text[:index].strip()
    reason = _reason_from_raw_text(text)
    if reason == STRUCTURE_REASON:
        # 历史前缀（如“LLM 输出不符合草稿结构：”）与新短句重复，只保留短句。
        return reason
    return f"{prefix}{reason}" if prefix else reason


def safe_failure_reason(exc: Exception, timeout_seconds: float | None = None) -> str:
    """写入数据库与审计的失败原因：模型错误走分类，其余只保留业务短消息。"""
    if is_provider_error(exc):
        return generation_failure_message(exc, timeout_seconds)
    return sanitize_failure_text(str(exc)) or "任务执行失败，请查看运行日志"


def schema_error_fields(exc: Exception, limit: int = 10) -> list[str]:
    """提取结构校验失败的字段路径与错误类型，供本地日志定位问题。

    只输出 `字段:错误类型`，不包含模型返回内容，因此可以安全写入本地日志。
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return []
    fields: list[str] = []
    for item in errors():
        if not isinstance(item, dict):
            continue
        location = ".".join(str(part) for part in item.get("loc", ()))
        fields.append(f"{location or '<root>'}:{item.get('type')}")
    return fields[:limit]
