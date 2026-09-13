from types import SimpleNamespace

import pytest

from app.config import api_key_for, base_url_for, model_for, structured_output_mode_for


@pytest.fixture(autouse=True)
def _isolate_from_stored_model_profiles(monkeypatch):
    """这些用例覆盖“环境变量回退链”，必须屏蔽数据库里的模型档案。

    真实情况：运营者在系统设置里把环境变量导入为模型档案后，
    `model_for()` 优先返回档案里的模型，这些用例会因为读到真实配置而失败。
    """
    monkeypatch.setattr("app.config._stored_model_profile_value", lambda *args, **kwargs: None)


def test_task_models_fall_back_to_legacy_llm_model() -> None:
    settings = SimpleNamespace(llm_model="legacy-model")

    assert model_for(settings, "conversation") == "legacy-model"
    assert model_for(settings, "content") == "legacy-model"
    assert model_for(settings, "review") == "legacy-model"
    assert model_for(settings, "illustration_planner") == "legacy-model"


def test_task_models_can_be_overridden_independently() -> None:
    settings = SimpleNamespace(
        llm_model="legacy-model",
        conversation_llm_model="chat-model",
        content_llm_model="writer-model",
        review_llm_model="reviewer-model",
        illustration_planner_llm_model="planner-model",
    )

    assert model_for(settings, "conversation") == "chat-model"
    assert model_for(settings, "content") == "writer-model"
    assert model_for(settings, "review") == "reviewer-model"
    assert model_for(settings, "illustration_planner") == "planner-model"


def test_task_base_urls_fall_back_and_can_be_overridden_independently() -> None:
    fallback = SimpleNamespace(openai_base_url="https://default.example/v1")
    override = SimpleNamespace(
        openai_base_url="https://default.example/v1",
        conversation_openai_base_url="https://chat.example/v1",
        content_openai_base_url="https://writer.example/v1",
        review_openai_base_url="https://review.example/v1",
        illustration_planner_openai_base_url="https://planner.example/v1",
    )

    assert [base_url_for(fallback, task) for task in ("conversation", "content", "review", "illustration_planner")] == ["https://default.example/v1"] * 4
    assert [base_url_for(override, task) for task in ("conversation", "content", "review", "illustration_planner")] == [
        "https://chat.example/v1",
        "https://writer.example/v1",
        "https://review.example/v1",
        "https://planner.example/v1",
    ]


def test_task_api_keys_fall_back_and_can_be_overridden_independently() -> None:
    fallback = SimpleNamespace(openai_api_key="default-key")
    override = SimpleNamespace(
        openai_api_key="default-key",
        conversation_openai_api_key="chat-key",
        content_openai_api_key="writer-key",
        review_openai_api_key="review-key",
        illustration_planner_openai_api_key="planner-key",
    )

    assert [api_key_for(fallback, task) for task in ("conversation", "content", "review", "illustration_planner")] == ["default-key"] * 4
    assert [api_key_for(override, task) for task in ("conversation", "content", "review", "illustration_planner")] == [
        "chat-key",
        "writer-key",
        "review-key",
        "planner-key",
    ]


def test_structured_output_mode_falls_back_and_can_be_overridden_per_task() -> None:
    # 旧配置没有新字段时保持强制工具调用，思考模式模型只需覆盖对应任务。
    assert structured_output_mode_for(SimpleNamespace(), "content") == "tool"
    assert structured_output_mode_for(SimpleNamespace(llm_structured_output_mode="json"), "content") == "json"

    override = SimpleNamespace(
        llm_structured_output_mode="tool",
        content_structured_output_mode="json",
        review_structured_output_mode="JSON",
    )

    assert structured_output_mode_for(override, "content") == "json"
    assert structured_output_mode_for(override, "review") == "json"
    assert structured_output_mode_for(override, "conversation") == "tool"


def test_structured_output_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="不支持的结构化输出模式"):
        structured_output_mode_for(SimpleNamespace(llm_structured_output_mode="xml"), "content")
