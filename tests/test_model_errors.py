"""失败原因必须脱敏：只显示分类短句，绝不回显供应商原始响应。"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.generator import GenerationError, ResilientDraftGenerator
from app.services.model_errors import (
    STRUCTURE_REASON,
    STRUCTURED_OUTPUT_REASON,
    generation_failure_message,
    safe_failure_reason,
    sanitize_failure_text,
)


class ProviderRejection(Exception):
    """替身：思考模式模型拒绝 tool_choice 的 400 响应，含供应商原始文本。"""

    def __init__(self, status_code: int = 400) -> None:
        super().__init__(
            f"Error code: {status_code} - {{'error': {{'code': 'invalid_parameter_error', "
            "'message': 'The tool_choice parameter does not support being set to required or object "
            "in thinking mode'}, 'id': 'chatcmpl-b140990b-1a4c-905f-bf08-25092feb88d7'}"
        )
        self.status_code = status_code
        self.body = {
            "error": {
                "code": "invalid_parameter_error",
                "param": None,
                "message": "The tool_choice parameter does not support being set to required or object "
                "in thinking mode",
                "type": "invalid_request_error",
            },
            "id": "chatcmpl-b140990b-1a4c-905f-bf08-25092feb88d7",
        }


class APITimeoutError(Exception):
    """替身：openai SDK 的超时异常（名字里带 APITimeoutError）。"""


class APIConnectionError(Exception):
    """替身：openai SDK 的连接异常。"""


class OpenAITimeoutError(APITimeoutError):
    """替身：langchain-openai 在 SDK 异常外再包一层，最外层类名不含 APITimeoutError。"""


class OpenAIConnectionError(APIConnectionError):
    """替身：langchain-openai 的连接异常包装。"""


def test_langchain_wrapped_timeout_is_recognized_as_provider_error() -> None:
    """只看最外层类名会漏判，原始英文 'Request timed out.' 就会直接写进界面。"""
    reason = safe_failure_reason(OpenAITimeoutError("Request timed out."), timeout_seconds=600)
    connection = safe_failure_reason(OpenAIConnectionError("Connection error."))

    assert reason == "内容模型请求超时（600 秒）"
    assert "Request timed out" not in reason
    assert connection == "内容模型服务连接失败"


def test_business_errors_are_not_mistaken_for_provider_errors() -> None:
    assert safe_failure_reason(GenerationError("草稿包含不存在的证据引用")) == "草稿包含不存在的证据引用"
    assert safe_failure_reason(ValueError("正文至少需要 1200 个字符")) == "正文至少需要 1200 个字符"


def test_thinking_mode_tool_choice_error_becomes_actionable_reason() -> None:
    reason = generation_failure_message(ProviderRejection())

    assert reason == STRUCTURED_OUTPUT_REASON
    assert "思考模式" in reason and "json" in reason
    for leaked in ("Error code", "chatcmpl", "tool_choice", "invalid_parameter_error"):
        assert leaked not in reason


def test_status_specific_reasons_never_include_provider_text() -> None:
    cases = {
        402: "额度不足",
        401: "凭据",
        403: "凭据",
        404: "名称",
        429: "限流",
        500: "异常状态",
    }

    for status, expected in cases.items():
        reason = generation_failure_message(ProviderRejection(status))
        assert expected in reason, (status, reason)
        assert "chatcmpl" not in reason
        assert "Error code" not in reason


def test_timeout_reason_includes_the_configured_limit() -> None:
    assert generation_failure_message(TimeoutError("read timeout"), 600) == "内容模型请求超时（600 秒）"
    assert generation_failure_message(TimeoutError("read timeout")) == "内容模型请求超时"


def test_resilient_generator_wraps_provider_error_without_raw_text() -> None:
    class RejectingPrimary:
        settings = SimpleNamespace(content_llm_timeout_seconds=600)

        def generate(self, _item):
            raise ProviderRejection()

    item = SimpleNamespace(source_kind=SimpleNamespace(value="github"), external_id="owner/repo")

    try:
        ResilientDraftGenerator(RejectingPrimary()).generate(item)
    except GenerationError as exc:
        assert "思考模式" in str(exc)
        assert "chatcmpl" not in str(exc)
    else:
        raise AssertionError("供应商错误必须转为 GenerationError")


def test_resilient_generator_keeps_non_provider_bugs_visible() -> None:
    class BrokenPrimary:
        def generate(self, _item):
            raise AttributeError("'NoneType' object has no attribute 'body'")

    item = SimpleNamespace(source_kind=SimpleNamespace(value="github"), external_id="owner/repo")

    try:
        ResilientDraftGenerator(BrokenPrimary()).generate(item)
    except AttributeError:
        pass
    else:
        raise AssertionError("代码缺陷不应被伪装成模型错误")


def test_sanitize_failure_text_rewrites_a_stored_raw_error() -> None:
    stored = (
        "生成失败：Error code: 400 - {'error': {'code': 'invalid_parameter_error', 'param': None, "
        "'message': 'The tool_choice parameter does not support being set to required or object in "
        "thinking mode', 'type': 'invalid_request_error'}, "
        "'id': 'chatcmpl-b140990b-1a4c-905f-bf08-25092feb88d7'}"
    )

    cleaned = sanitize_failure_text(stored)

    assert cleaned == f"生成失败：{STRUCTURED_OUTPUT_REASON}"
    assert "chatcmpl" not in cleaned


def test_sanitize_failure_text_rewrites_a_pydantic_dump() -> None:
    stored = (
        "生成失败：LLM 输出不符合草稿结构：1 validation error for DraftContent\ncard_script\n"
        "  Input should be a valid list [type=list_type, input_value='把自然语言规范变...', input_type=str]\n"
        "    For further information visit https://errors.pydantic.dev/2.13/v/list_type"
    )

    cleaned = sanitize_failure_text(stored)

    assert cleaned == STRUCTURE_REASON
    assert "validation error" not in cleaned
    assert "pydantic" not in cleaned


def test_sanitize_failure_text_keeps_clean_text_and_caps_long_text() -> None:
    assert sanitize_failure_text("采集完成：新增 3 条待审核草稿") == "采集完成：新增 3 条待审核草稿"
    assert sanitize_failure_text("内容模型输出不符合草稿结构，请重试或检查模型配置") == (
        "内容模型输出不符合草稿结构，请重试或检查模型配置"
    )
    assert sanitize_failure_text("") == ""
    assert sanitize_failure_text(None) == ""

    long_text = "说明" * 400
    capped = sanitize_failure_text(long_text)
    assert len(capped) <= 300
    assert capped.endswith("…")


def test_safe_failure_reason_prefers_classification_only_for_provider_errors() -> None:
    assert safe_failure_reason(ProviderRejection()) == STRUCTURED_OUTPUT_REASON
    # 业务校验类异常的短消息可以保留，它不含供应商原文。
    assert safe_failure_reason(ValueError("正文至少需要 1200 个字符")) == "正文至少需要 1200 个字符"
