"""任务结束后的对话汇报由模型生成，而不是写死模板。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.task_narration as narration


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "llm_enabled": True,
        "llm_model": "conversation-model",
        "conversation_llm_model": None,
        "openai_api_key": "key",
        "conversation_openai_api_key": None,
        "openai_base_url": "https://example.invalid/v1",
        "conversation_openai_base_url": None,
        "conversation_agent_timeout_seconds": 30,
        "langsmith_tracing": False,
        "langsmith_api_key": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _fake_client(content: str = "草稿已生成。要现在运行自动审核吗？回复“审核”即可。", *, raises: bool = False, captured: dict | None = None):
    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            if raises:
                raise RuntimeError("model down")
            if captured is not None:
                captured["messages"] = kwargs["messages"]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    return FakeClient


def test_task_reply_is_written_by_the_model_from_result_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(narration, "OpenAI", _fake_client(captured=captured))

    reply = narration.compose_task_reply(
        _settings(),
        task="采集并生成草稿",
        facts={"drafts_created": 1, "auto_review_requested": False, "image_tasks_created": 4},
        fallback="固定文案",
        run_id="run-1",
    )

    assert reply == "草稿已生成。要现在运行自动审核吗？回复“审核”即可。"
    system_prompt = captured["messages"][0]["content"]
    user_prompt = captured["messages"][1]["content"]
    # 事实进入提示词，约束也进入提示词：不得编造、只在需要用户决定时追问。
    assert '"drafts_created": 1' in user_prompt
    assert "不得编造结果" in system_prompt
    assert "只有当结果里存在需要用户决定的事项时" in system_prompt


def test_task_reply_falls_back_when_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narration, "OpenAI", _fake_client(raises=True))

    reply = narration.compose_task_reply(
        _settings(), task="重写正文", facts={"new_version": 6}, fallback="正文已更新为版本 6。"
    )

    assert reply == "正文已更新为版本 6。"


def test_task_reply_falls_back_without_model_configuration() -> None:
    reply = narration.compose_task_reply(
        _settings(llm_enabled=False), task="采集", facts={}, fallback="回退文案"
    )

    assert reply == "回退文案"


def test_task_reply_falls_back_on_empty_model_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narration, "OpenAI", _fake_client("   "))

    reply = narration.compose_task_reply(_settings(), task="采集", facts={}, fallback="回退文案")

    assert reply == "回退文案"


def test_worker_reports_task_result_through_the_model() -> None:
    import inspect

    from app import worker

    source = inspect.getsource(worker)
    assert "compose_task_reply" in source
    # 三处任务结束点都应交给模型汇报，而不是写死询问。
    assert source.count("compose_task_reply") >= 4  # import + 三处调用
    assert "要现在运行一次自动审核吗" in source  # 仅作为模型不可用时的回退文案
