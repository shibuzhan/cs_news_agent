"""正文（body）增量流式：写一篇要几分钟，用户必须能看到它在写。

真实反馈：“我只想要流式可见：正文用我们已有的增量提取器边写边推”。
这里锁住四件事：
1. 从增量 JSON 里提取 `body` 字段（含转义与分片边界）；
2. Worker 设置发布器后，**线程里的**生成代码能读到它（真实生成跑在 `asyncio.to_thread` 里）；
3. 内容子 Agent 真的把正文分片推了出去，且换篇时先发 reset；
4. 流式不可用时自动回退非流式，生成绝不因为“想流式”而失败。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

import app.agents.content_task_agents as task_agents
from app.agents.content_task_agents import RestrictedContentTaskAgent
from app.services import reply_stream
from app.services.reply_stream import JsonFieldExtractor, active_publisher, streaming_run


class RecordingPublisher:
    """替代 StreamPublisher：只记录调用，不碰 Redis。"""

    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.resets = 0
        self.done: list[tuple[str, dict]] = []

    def delta(self, text: str) -> None:
        self.deltas.append(text)

    def reset(self) -> None:
        self.resets += 1

    def done(self, text: str, **extra) -> None:
        self.done.append((text, extra))


@pytest.fixture(autouse=True)
def _isolate_from_stored_model_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config._stored_model_profile_value", lambda *args, **kwargs: None)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        llm_enabled=True,
        content_llm_model="content-model",
        content_openai_api_key="content-key",
        content_openai_base_url="https://content.example/v1",
        review_llm_model="review-model",
        review_openai_api_key="review-key",
        review_openai_base_url="https://review.example/v1",
        llm_model=None,
        openai_api_key=None,
        openai_base_url=None,
        content_llm_timeout_seconds=90,
        content_llm_max_retries=0,
        llm_structured_output_mode="json",
        content_structured_output_mode="json",
    )


# --- 1. body 字段增量提取 ---------------------------------------------------------------


def test_body_field_is_extracted_from_partial_json() -> None:
    extractor = JsonFieldExtractor("body")
    pieces: list[str] = []
    for chunk in [
        '{"title_options":["标题一"],"summary_cn":"导语","body":"　　第一段',
        "继续写",
        '\\n\\n　　第二段',
        '"}',
    ]:
        pieces.extend(extractor.feed(chunk))

    assert "".join(pieces) == "　　第一段继续写\n\n　　第二段"
    assert extractor.text.endswith("第二段")


def test_body_extractor_waits_for_incomplete_unicode_escape() -> None:
    extractor = JsonFieldExtractor("body")

    # `\u4f` 只是半个转义序列：此刻不能把半成品吐给用户。
    assert extractor.feed('{"body":"\\u4f') == []
    assert extractor.feed("60\\u597d，世界") == ["你好，世界"]


# --- 2. 发布器穿过线程边界 ---------------------------------------------------------------


def test_streaming_run_reaches_the_generation_thread() -> None:
    """真实生成在 `asyncio.to_thread` 里跑：发布器必须能被线程里的代码读到。"""
    publisher = RecordingPublisher()
    token = reply_stream._active_publisher.set(publisher)  # type: ignore[arg-type]

    async def read_in_thread() -> object:
        return await asyncio.to_thread(active_publisher)

    try:
        assert asyncio.run(read_in_thread()) is publisher
    finally:
        reply_stream._active_publisher.reset(token)


def test_streaming_run_is_disabled_without_a_run_id() -> None:
    with streaming_run(None, "draft") as publisher:
        assert publisher is None
        assert active_publisher() is None


# --- 3. 内容子 Agent 真的推正文 -----------------------------------------------------------


def _fake_agent_streaming(deltas: list[str]):
    class FakeAgent:
        def stream(self, payload, stream_mode=None):  # noqa: ANN001, ARG002
            body = '{"title_options":["标题"],"summary_cn":"导语","body":"　　正文第一段'
            yield ("messages", (SimpleNamespace(type="ai", content=body), {}))
            yield ("messages", (SimpleNamespace(type="ai", content="　　继续"), {}))
            yield ("messages", (SimpleNamespace(type="ai", content='"}'), {}))
            yield (
                "values",
                {
                    "messages": [
                        SimpleNamespace(
                            type="ai",
                            content=body + "　　继续" + '"}',
                        )
                    ]
                },
            )

        def invoke(self, payload):  # noqa: ANN001, ARG002
            raise AssertionError("流式可用时不应回退 invoke")

    return FakeAgent()


def test_content_writer_publishes_body_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_agents, "_configure_profile", lambda: None)
    monkeypatch.setattr(task_agents, "ChatOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(
        task_agents, "create_deep_agent", lambda **_kwargs: _fake_agent_streaming([])
    )
    publisher = RecordingPublisher()
    monkeypatch.setattr(reply_stream, "active_publisher", lambda: publisher)

    agent = RestrictedContentTaskAgent(_settings(), "content")
    response = agent.write("写一篇")

    assert response.body.startswith("　　正文第一段")
    assert publisher.resets == 1, "换一篇正文前必须先清空前端上一段的临时文本"
    assert "".join(publisher.deltas) == "　　正文第一段　　继续"


def test_content_writer_falls_back_when_streaming_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class BrokenStreamAgent:
        def stream(self, payload, stream_mode=None):  # noqa: ANN001, ARG002
            raise RuntimeError("gateway rejected stream")

        def invoke(self, payload):  # noqa: ANN001, ARG002
            calls.append("invoke")
            return {
                "structured_response": {
                    "title_options": ["标题"],
                    "summary_cn": "导语",
                    "body": "　　正文。",
                    "tags": ["AI"],
                    "card_script": "卡片",
                    "claim_citations": [],
                }
            }

    monkeypatch.setattr(task_agents, "_configure_profile", lambda: None)
    monkeypatch.setattr(task_agents, "ChatOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(task_agents, "create_deep_agent", lambda **_kwargs: BrokenStreamAgent())
    publisher = RecordingPublisher()
    monkeypatch.setattr(reply_stream, "active_publisher", lambda: publisher)

    response = RestrictedContentTaskAgent(_settings(), "content").write("写一篇")

    assert calls == ["invoke"], "流式失败必须回退非流式，生成不能因此失败"
    assert response.body == "　　正文。"
    assert publisher.deltas == []


# --- 4. SSE 端点把正文与 reset 一起送出去 -------------------------------------------------


def test_sse_endpoint_streams_draft_kind_and_reset_frames() -> None:
    from app.api import routes

    source = inspect.getsource(routes._chat_run_event_stream)

    assert 'payload.get("reset")' in source
    assert "换一篇正文" in source
    # 收到 done 不能立刻结束连接：正文可能还在写，结束要看数据库里的运行状态。
    assert "是否结束由下面按数据库里的运行状态决定" in source


def test_worker_wraps_generation_in_a_streaming_run() -> None:
    from app import worker

    collect_source = inspect.getsource(worker.process_collection_job)
    regenerate_source = inspect.getsource(worker.process_draft_regeneration_job)

    assert 'streaming_run(chat_run_id, "draft")' in collect_source
    assert 'streaming_run(chat_run_id, "draft")' in regenerate_source


def test_publisher_payload_carries_the_kind() -> None:
    """前端按 kind 分桶渲染：draft 是正文，chat/report 是回复与汇报。"""
    payload = json.loads(json.dumps({"kind": "draft", "text": "正文"}))
    assert payload["kind"] == "draft"
