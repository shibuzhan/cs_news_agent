"""将模型 SDK 异常转换为可审计、无敏感信息的运营提示。"""

from __future__ import annotations


def model_failure_message(exc: Exception, operation: str, timeout_seconds: float) -> str:
    """不返回供应商原始响应、URL、请求头或密钥。"""
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
