"""流式（SSE）三件事必须成立：增量可用、结束给完整文本、失败自动回退非流式。

结构化路径（生成/审核/改稿）要求完整 JSON，绝不能改成流式解析，这里也一并锁住。
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from app.services import reply_stream, task_narration
from app.services.reply_stream import ReplyFieldExtractor, StreamPublisher


# --- 增量字段提取 ---------------------------------------------------------------------


def test_reply_field_is_streamed_across_chunks() -> None:
    extractor = ReplyFieldExtractor()
    pieces: list[str] = []
    for chunk in ['{"intent":"general_chat","reply":"你好', "，世界", '"}']:
        pieces.extend(extractor.feed(chunk))

    assert "".join(pieces) == "你好，世界"
    assert extractor.text == "你好，世界"


def test_incomplete_escape_waits_for_the_next_chunk() -> None:
    extractor = ReplyFieldExtractor()
    # 反斜杠刚好落在分片边界：不能把裸反斜杠推给用户，必须等下一个分片。
    first = extractor.feed('{"reply":"第一行\\')
    second = extractor.feed('n第二行"}')

    assert first == ["第一行"]
    assert second == ["\n第二行"]
    assert extractor.text == "第一行\n第二行"


def test_unicode_escape_split_across_chunks_is_decoded_once() -> None:
    extractor = ReplyFieldExtractor()
    pieces: list[str] = []
    for chunk in ['{"reply":"\\u4f60', '\\u597d"}']:
        pieces.extend(extractor.feed(chunk))

    assert "".join(pieces) == "你好"


def test_tool_call_arguments_are_extracted_too() -> None:
    """tool 模式下决策是工具参数 JSON，reply 必须同样能增量提取。"""
    extractor = ReplyFieldExtractor()
    pieces: list[str] = []
    for chunk in ['{"reply": "已', "开始生成封面", '"}']:
        pieces.extend(extractor.feed(chunk))

    assert "".join(pieces) == "已开始生成封面"


def test_extractor_ignores_text_before_the_reply_field() -> None:
    extractor = ReplyFieldExtractor()

    assert extractor.feed('{"intent": "general_chat", ') == []
    assert extractor.feed('"reply": "好的"') == ["好的"]


# --- 发布端 ---------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.buffers: dict[str, str] = {}
        self.published: list[tuple[str, dict]] = []

    def set(self, key: str, value: str, ex: int | None = None) -> None:  # noqa: ARG002
        self.buffers[key] = value

    def append(self, key: str, value: str) -> None:
        self.buffers[key] = self.buffers.get(key, "") + value

    def expire(self, key: str, seconds: int) -> None:  # noqa: ARG002
        return None

    def get(self, key: str):
        return self.buffers.get(key)

    def delete(self, key: str) -> None:
        self.buffers.pop(key, None)

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, json.loads(payload)))


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(reply_stream, "_client", client, raising=False)
    monkeypatch.setattr(reply_stream, "_sync_client", lambda: client)
    return client


def test_publisher_accumulates_the_buffer_and_publishes_done(fake_redis: FakeRedis) -> None:
    publisher = StreamPublisher("run-1", "chat")

    publisher.delta("你好")
    publisher.delta("，世界")
    publisher.done("你好，世界", status="completed")

    kinds = [payload["kind"] for _, payload in fake_redis.published]
    assert kinds == ["chat", "chat", "chat"]
    types = [payload["type"] for _, payload in fake_redis.published]
    assert types == ["delta", "delta", "done"]
    assert fake_redis.published[-1][1]["text"] == "你好，世界"
    # 结束帧之后缓冲被清掉：晚订阅的客户端会以数据库里的最终消息为准。
    assert reply_stream.read_buffer("run-1") == ""


def test_publish_never_raises_on_broken_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenRedis:
        def __getattr__(self, _name):  # noqa: ANN401
            raise RuntimeError("redis down")

    monkeypatch.setattr(reply_stream, "_sync_client", lambda: BrokenRedis())

    # 推送失败只记日志：不能把回复或后台任务带崩。
    reply_stream.publish("chat", "run-1", text="片段")
    assert reply_stream.read_buffer("run-1") == ""


# --- 汇报生成的流式/回退 ---------------------------------------------------------------


def test_streaming_task_reply_pushes_deltas() -> None:
    class FakeCompletions:
        def create(self, **_kwargs):
            return [
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="审核"))]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="通过。"))]),
            ]

    settings = SimpleNamespace(llm_enabled=True, conversation_agent_timeout_seconds=10)
    seen: list[str] = []
    sender = StreamPublisher("run-1", "report")
    sender.delta = seen.append  # type: ignore[method-assign]

    import app.services.task_narration as narration

    original_client = narration._client
    original_model = narration.model_for
    original_key = narration.api_key_for
    narration._client = lambda _settings: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    narration.model_for = lambda *_args, **_kwargs: "test-model"
    narration.api_key_for = lambda *_args, **_kwargs: "test-key"
    try:
        reply = task_narration.compose_task_reply_streaming(
            settings, task="审核", facts={}, fallback="兜底", run_id="run-1", on_delta=seen.append
        )
    finally:
        narration._client = original_client
        narration.model_for = original_model
        narration.api_key_for = original_key

    assert reply == "审核通过。"
    assert seen == ["审核", "通过。"]


def test_streaming_failure_falls_back_to_non_streaming() -> None:
    settings = SimpleNamespace(llm_enabled=True, conversation_agent_timeout_seconds=10)
    calls: list[str] = []

    def broken_client(_settings):
        raise RuntimeError("gateway rejected stream")

    def fake_non_streaming(_settings, *, task, facts, fallback, run_id=""):  # noqa: ANN001
        calls.append(task)
        return "非流式兜底文本"

    import app.services.task_narration as narration

    original_client, original_compose = narration._client, narration.compose_task_reply
    original_model, original_key = narration.model_for, narration.api_key_for
    narration._client = broken_client
    narration.compose_task_reply = fake_non_streaming
    narration.model_for = lambda *_args, **_kwargs: "test-model"
    narration.api_key_for = lambda *_args, **_kwargs: "test-key"
    try:
        reply = task_narration.compose_task_reply_streaming(
            settings, task="投递", facts={}, fallback="兜底", run_id="run-1", on_delta=lambda _t: None
        )
    finally:
        narration._client, narration.compose_task_reply = original_client, original_compose
        narration.model_for, narration.api_key_for = original_model, original_key

    assert reply == "非流式兜底文本"
    assert calls == ["投递"]


# --- 边界：结构化路径不流式 -------------------------------------------------------------


def test_structured_paths_keep_using_non_streaming_calls() -> None:
    """生成/审核/改稿依赖完整 JSON：这些模块里不允许出现 stream=True。"""
    from app.services import evidence_selector, generator
    from app.tools import auto_review, auto_revision

    for module in (generator, auto_review, auto_revision, evidence_selector):
        assert "stream=True" not in inspect.getsource(module), f"{module.__name__} 不应改成流式"


def test_worker_reports_through_the_streaming_helper() -> None:
    from app import worker

    source = inspect.getsource(worker)

    assert "compose_task_reply_streaming" in inspect.getsource(worker._compose_report)
    # 所有汇报路径都必须走统一入口（新增按审核意见改稿后是 5 条），避免又出现“某条路径不会流式”的漏网。
    assert source.count("await _compose_report(") == 5
