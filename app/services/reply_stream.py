"""对话回复与任务汇报的**增量推送**通道（SSE 的服务端一半）。

设计取舍：
- Worker 里生成回复的代码在工作线程中运行，所以发布端用**同步** Redis 客户端；
  FastAPI 侧用 `redis.asyncio` 订阅，两边只共享一个频道名。
- Redis 不可用时发布是 no-op、订阅立即失败：端点会退化成按数据库轮询，
  用户最终仍然拿到完整文本（真实要求：失败自动回退非流式）。
- 已推送的文本同时写进一个带 TTL 的缓冲键，晚连上来的客户端也能补齐前面的分片。
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger("news_agent.reply_stream")

CHANNEL_PREFIX = "chat:stream:"
BUFFER_PREFIX = "chat:stream:buffer:"
# 运行结束后缓冲还要留一会儿：前端可能在 done 之后才连上来补历史。
BUFFER_TTL_SECONDS = 300
_SOCKET_TIMEOUT_SECONDS = 2.0

_client = None


def channel_for(run_id: str) -> str:
    return f"{CHANNEL_PREFIX}{run_id}"


def buffer_key_for(run_id: str) -> str:
    return f"{BUFFER_PREFIX}{run_id}"


def stream_enabled() -> bool:
    """流式是增强能力：默认开启，可用 NEWS_AGENT_CHAT_STREAM=0 关掉。"""
    import os

    return os.environ.get("NEWS_AGENT_CHAT_STREAM", "1") not in {"0", "false", "False"}


def _sync_client():
    global _client
    if _client is None:
        import redis

        from app.config import get_settings

        _client = redis.Redis.from_url(
            get_settings().redis_url,
            socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        )
    return _client


def publish(kind: str, run_id: str, *, text: str = "", done: bool = False, **extra) -> None:
    """发布一个增量分片或结束帧；任何失败都只记日志（绝不打断任务）。"""
    if not run_id or not stream_enabled():
        return
    payload = {"type": "done" if done else "delta", "kind": kind, "text": text, **extra}
    try:
        client = _sync_client()
        if text:
            if done:
                # 结束帧带的是完整文本：直接覆盖缓冲，晚订阅者拿到的也一定是完整版。
                client.set(buffer_key_for(run_id), text, ex=BUFFER_TTL_SECONDS)
            else:
                _append(client, run_id, text)
        client.publish(channel_for(run_id), json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 推送失败不能让回复或任务失败
        logger.warning("reply_stream_publish_failed run_id=%s kind=%s error_type=%s", run_id, kind, type(exc).__name__)


def _append(client, run_id: str, text: str) -> str:
    key = buffer_key_for(run_id)
    client.append(key, text)
    client.expire(key, BUFFER_TTL_SECONDS)
    value = client.get(key)
    return value.decode("utf-8") if isinstance(value, bytes) else (value or "")


def read_buffer(run_id: str) -> str:
    """读取已推送的累计文本（同步客户端，供端点首帧补齐）。"""
    try:
        value = _sync_client().get(buffer_key_for(run_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("reply_stream_buffer_read_failed run_id=%s error_type=%s", run_id, type(exc).__name__)
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def clear_buffer(run_id: str) -> None:
    try:
        _sync_client().delete(buffer_key_for(run_id))
    except Exception:  # noqa: BLE001 - 清理失败只影响下一次补齐
        logger.debug("reply_stream_buffer_clear_failed run_id=%s", run_id)


class JsonFieldExtractor:
    """从**增量输出**里推进式提取某个 JSON 字符串字段的可见文本。

    对话模型要么直接吐 JSON 文本（json 模式），要么把结构化结果作为工具参数吐出来
    （tool 模式）；两种情况下目标字段都是一个 JSON 字符串。这里只处理字符串值：
    转义未完成（行尾一个 `\\`）或 `\\uXXXX` 不完整时停在原地，等下一个分片。
    """

    def __init__(self, field: str = "reply") -> None:
        self._key = f'"{field}"'
        self.buffer = ""
        self._start: int | None = None
        self._emitted = 0

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self.buffer += chunk
        if self._start is None and not self._locate():
            return []
        visible = self._visible()
        if len(visible) <= self._emitted:
            return []
        piece = visible[self._emitted:]
        self._emitted = len(visible)
        return [piece]

    @property
    def text(self) -> str:
        return self._visible() if self._start is not None else ""

    def _locate(self) -> bool:
        index = self.buffer.find(self._key)
        if index < 0:
            return False
        colon = self.buffer.find(":", index + len(self._key))
        if colon < 0:
            return False
        quote = self.buffer.find('"', colon + 1)
        if quote < 0:
            return False
        self._start = quote + 1
        return True

    def _visible(self) -> str:
        assert self._start is not None
        raw = self.buffer[self._start:]
        out: list[str] = []
        index = 0
        while index < len(raw):
            char = raw[index]
            if char == '"':
                break
            if char == "\\":
                if index + 1 >= len(raw):
                    break  # 转义还没写完
                nxt = raw[index + 1]
                if nxt == "u":
                    if index + 6 > len(raw):
                        break  # \uXXXX 还不完整
                    try:
                        out.append(chr(int(raw[index + 2:index + 6], 16)))
                    except ValueError:
                        break
                    index += 6
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(nxt, nxt))
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)


class ReplyFieldExtractor(JsonFieldExtractor):
    """对话决策里的 `reply` 字段（保持既有名字，避免调用方大改）。"""

    def __init__(self) -> None:
        super().__init__("reply")


# 当前正在流式推送的运行：由 Worker 在调用长任务前设置，生成侧在**同一线程内**读取。
# `asyncio.to_thread` 会复制上下文，因此这里的值能穿过线程边界到达生成代码。
_active_publisher: ContextVar["StreamPublisher | None"] = ContextVar(
    "reply_stream_publisher", default=None
)


def active_publisher() -> "StreamPublisher | None":
    """返回当前运行的发布器；没有开启流式时返回 None。"""
    return _active_publisher.get()


@contextmanager
def streaming_run(run_id: str | None, kind: str = "draft"):
    """在这个上下文里执行的任务，会把增量发布到 `run_id` 的事件流。"""
    publisher = StreamPublisher(run_id, kind) if run_id and stream_enabled() else None
    token = _active_publisher.set(publisher)
    try:
        yield publisher
    finally:
        _active_publisher.reset(token)


class StreamPublisher:
    """把增量发布包装成可调用对象；同一个运行只发一次 `done`。"""

    def __init__(self, run_id: str, kind: str) -> None:
        self.run_id = run_id
        self.kind = kind
        self._closed = False
        self._started_at = time.monotonic()

    def delta(self, text: str) -> None:
        if text and not self._closed:
            publish(self.kind, self.run_id, text=text)

    def reset(self) -> None:
        """清空前端这一类别的临时文本（多来源采集会连续写多篇正文）。"""
        if not self._closed:
            publish(self.kind, self.run_id, reset=True)

    def done(self, text: str, **extra) -> None:
        if self._closed:
            return
        self._closed = True
        publish(self.kind, self.run_id, text=text, done=True, **extra)
        clear_buffer(self.run_id)
