"""`append_run_reply` 这类仓储方法必须真的能跑：占位消息保留、结果追加、指针前移。

真实故障：`append_run_reply` 里写成 `self.flush()`（仓储没有这个方法），
于是每一次后台汇报都在最后一步抛 AttributeError：消息写不进去、运行也结束不了，
对话页就一直显示“处理中”（真实反馈：已完成的任务下方状态没有更新）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.storage.repositories import ContentRepository, RepositoryError


class FakeSession:
    """只实现 append_run_reply 用到的那几个 SQLAlchemy Session 行为。"""

    def __init__(self, run, anchor) -> None:
        self.run = run
        self.anchor = anchor
        self.added: list[object] = []
        self.flushes = 0

    def get(self, model, key):  # noqa: ANN001 - 与 Session.get 同形
        if key == self.run.id:
            return self.run
        if key == self.anchor.id:
            return self.anchor
        return None

    def add(self, row) -> None:  # noqa: ANN001
        row.id = f"message-{len(self.added) + 2}"
        self.added.append(row)

    def flush(self) -> None:
        self.flushes += 1


def _run(**overrides):
    base = {
        "id": "run-1",
        "request_message_id": "message-1",
        "response_message_id": "message-1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_append_run_reply_keeps_the_anchor_and_moves_the_pointer() -> None:
    run = _run()
    anchor = SimpleNamespace(id="message-1", session_id="session-1")
    session = FakeSession(run, anchor)
    repository = ContentRepository(session)

    message_id = repository.append_run_reply("run-1", "配图已完成（3 张）。")

    assert message_id == "message-2"
    appended = session.added[0]
    assert appended.session_id == "session-1"
    assert appended.role == "assistant"
    assert appended.content == "配图已完成（3 张）。"
    # 占位消息（受理回执）保持不动，指针前移到新消息。
    assert run.response_message_id == "message-2"
    # 两次 flush 都必须落在 session 上：仓储本身没有 flush()。
    assert session.flushes == 2


def test_append_run_reply_without_anchor_is_a_clear_error() -> None:
    run = _run(request_message_id=None, response_message_id=None)
    repository = ContentRepository(FakeSession(run, SimpleNamespace(id="x")))

    with pytest.raises(RepositoryError):
        repository.append_run_reply("run-1", "结果")
