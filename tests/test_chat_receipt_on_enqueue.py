"""入队即回执：对话接口不调模型、不执行工具，写完成确定性回执就返回。

真实反馈：“为什么直到任务结束才响应聊天”。这里锁住三件事：
1. 回执文案由 `plan_chat_message()` 确定性产出（界面命令 / 多步计划 / 问句兜底都不猜错）；
2. 回执在**入队之前**就已经落库，所以前端下一次轮询就能看到；
3. 接口返回的 payload 里带上了回执与运行卡片，前端不必等模型。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.api import routes
from app.domain.models import ChatMessageCreate, ConversationIntent
from app.services import chat_receipts
from app.services.chat_receipts import plan_chat_message


# --- 确定性计划 -----------------------------------------------------------------------


def test_multi_step_command_keeps_the_user_order() -> None:
    """两条命令必须都执行，且顺序按用户说的来（真实反馈：只跑了配图那条）。"""
    plan = plan_chat_message("openai/plugins：OpenAI 给出的 Codex 插件示例库\n帮我给这篇文章配图，然后自动审核")

    assert plan.steps == ("image", "review")
    assert plan.multi is True
    assert "1）生成配图" in plan.text and "2）自动审核" in plan.text
    assert plan.intent == ConversationIntent.GENERATE_DRAFT_IMAGE


def test_waiting_review_phrase_is_not_an_audit_command() -> None:
    """“生成待审核草稿”是名词短语，不能被当成“运行审核”。"""
    plan = plan_chat_message("获取 affaan-m/ECC 项目并生成待审核草稿")

    assert plan.steps == ("collect",)
    assert plan.multi is False


def test_question_is_never_reported_as_an_action() -> None:
    """问句不能回执成“已识别为采集/配图”，否则第一句话就在骗用户。"""
    plan = plan_chat_message("为什么今天没有新增草稿？")

    assert plan.steps == ("chat",)
    assert "正在理解" in plan.text


def test_ui_button_command_maps_to_a_single_step_without_the_model() -> None:
    plan = plan_chat_message("运行自动审核｜draft=7a0e6ed6-1111-2222")

    assert plan.steps == ("review",)
    assert plan.intent == ConversationIntent.RUN_AUTO_REVIEW
    assert "自动审核" in plan.text


def test_review_only_button_receipt_says_the_body_stays_untouched() -> None:
    """“仅运行审核（只出意见，不改稿）”的回执必须写明不改稿，否则用户以为正文已被改过。"""
    plan = plan_chat_message("仅运行审核｜draft=7a0e6ed6-1111-2222")

    assert plan.steps == ("review",)
    assert "不改稿" in plan.text
    assert plan.status == "正在审核（不改稿）"


def test_reselect_command_is_a_registered_step() -> None:
    """界面按钮“重新选择配图”也要走确定性回执，而不是退化到关键词扫描。"""
    plan = plan_chat_message("重新选择配图｜draft=7a0e6ed6-1111-2222")

    assert plan.steps == ("deliver",)
    assert plan.intent == ConversationIntent.PUBLISH_TO_WECHAT_DRAFT


def test_attachment_message_gets_an_extraction_receipt() -> None:
    plan = plan_chat_message("提取附件并生成待审核草稿", has_attachment=True)

    assert plan.intent == ConversationIntent.ATTACHMENT_DRAFT
    assert "附件" in plan.text


def test_plan_text_is_short_enough_for_a_status_card() -> None:
    for text, attached in [
        ("帮我采集今天的资讯", False),
        ("给这篇文章配图", False),
        ("帮我给这篇文章配图，然后自动审核", False),
        ("随便聊聊", False),
        ("提取附件内容", True),
    ]:
        plan = plan_chat_message(text, has_attachment=attached)
        # 运行卡片标题直接显示 status：必须短，不能是整段说明（真实反馈：详情太啰嗦）。
        assert len(plan.status) <= 40


# --- 接口行为（假仓储，不需要数据库） ---------------------------------------------------


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class FakeRepository:
    """只记录调用顺序的最小假仓储。"""

    instances: list["FakeRepository"] = []

    def __init__(self, session) -> None:  # noqa: ANN001 - 与真实仓储同形
        self.session = session
        self.messages: list[SimpleNamespace] = []
        self.events: list[dict] = []
        self.run: SimpleNamespace | None = None
        self.calls: list[str] = []
        FakeRepository.instances.append(self)

    def create_chat_message(self, session_id: str, role: str, content: str) -> SimpleNamespace:
        row = SimpleNamespace(
            id=f"message-{len(self.messages) + 1}", session_id=session_id, role=role,
            content=content, created_at=None,
        )
        self.messages.append(row)
        self.calls.append(f"message:{role}")
        return row

    def set_first_instruction_title(self, *_args, **_kwargs) -> None:
        self.calls.append("title")

    def update_chat_session_memory(self, *_args, **_kwargs) -> None:
        self.calls.append("memory")

    def create_chat_agent_run(
        self, session_id: str, request_message_id: str, intent, auto_review: bool, auto_illustration: bool,
    ) -> SimpleNamespace:
        run = SimpleNamespace(
            id="run-1", session_id=session_id, request_message_id=request_message_id,
            intent=getattr(intent, "value", str(intent)), status="running", summary="",
            response_message_id=None, auto_review_requested=auto_review,
            auto_illustration_requested=auto_illustration, created_at=None, finished_at=None,
        )
        self.run = run
        self.calls.append("run")
        return run

    def add_chat_agent_event(self, *_args, **_kwargs) -> None:
        self.calls.append("event")

    def get_draft(self, draft_id: str):
        return SimpleNamespace(id=draft_id, title_options_json=["openai/plugins：示例标题"])

    def finish_chat_agent_run(self, *_args, **_kwargs) -> None:
        self.calls.append("finish")


@pytest.fixture()
def fake_endpoint(monkeypatch: pytest.MonkeyPatch):
    enqueued: list[dict] = []
    FakeRepository.instances = []
    monkeypatch.setattr(routes, "ContentRepository", FakeRepository)
    monkeypatch.setattr(routes, "chat_message_to_dict", lambda row, *a, **k: {"id": row.id, "role": row.role, "content": row.content})
    monkeypatch.setattr(routes, "chat_agent_run_to_dict", lambda row, *a, **k: {"id": row.id, "status": row.status, "summary": row.summary})

    async def fake_enqueue(settings, run_id, session_id, content, attachment_id=None, auto_review=False, auto_illustration=False):
        repository = FakeRepository.instances[-1]
        enqueued.append(
            {
                "run_id": run_id,
                "content": content,
                # 入队这一刻，回执必须已经在库里：这正是“入队即回执”的判据。
                "messages_at_enqueue": [message.content for message in repository.messages],
            }
        )
        return "job-1"

    monkeypatch.setattr(routes, "enqueue_general_chat_job", fake_enqueue)
    return enqueued


def test_send_message_writes_the_receipt_before_enqueueing(fake_endpoint) -> None:
    session = FakeSession()
    command = ChatMessageCreate(content="帮我给这篇文章配图，然后自动审核")

    payload = asyncio.run(routes.send_chat_message("session-1", command, SimpleNamespace(), session))

    assert len(fake_endpoint) == 1
    receipt_texts = fake_endpoint[0]["messages_at_enqueue"]
    assert len(receipt_texts) == 2, "用户消息之后必须已经写好回执"
    assert receipt_texts[0] == "帮我给这篇文章配图，然后自动审核"
    assert "自动审核" in receipt_texts[1]
    # 返回值同时带回回执与运行卡片；用户消息仍用于替换前端的乐观消息。
    assert payload["message"]["role"] == "user"
    assert payload["receipt"]["content"] == receipt_texts[1]
    assert payload["execution"]["status"] == "running"
    assert session.commits == 1


def test_receipt_names_the_article_by_title_not_by_id(fake_endpoint) -> None:
    """用户反馈：返回的信息应该是稿件标题，而不是那串 draft id。"""
    command = ChatMessageCreate(content="重写文案｜draft=11111111-2222-3333-4444-555555555555")

    payload = asyncio.run(routes.send_chat_message("session-1", command, SimpleNamespace(), FakeSession()))

    receipt = payload["receipt"]["content"]
    assert "《openai/plugins：示例标题》" in receipt
    assert "11111111-2222-3333-4444-555555555555" not in receipt


def test_send_message_never_touches_a_model(fake_endpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    """接口里不允许再出现模型调用：一旦有人加回来，这条测试就会失败。"""

    class ExplodingAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("对话接口不得调用模型")

    monkeypatch.setattr(routes, "ContentDeepAgent", ExplodingAgent)
    monkeypatch.setattr(routes, "ChatAgent", ExplodingAgent)

    payload = asyncio.run(
        routes.send_chat_message("session-1", ChatMessageCreate(content="你好"), SimpleNamespace(), FakeSession())
    )

    assert payload["receipt"]["content"] == chat_receipts._single_step_receipt("chat")[0]


def test_plan_event_lists_the_whole_plan_for_the_card(fake_endpoint) -> None:
    plan = plan_chat_message("给这篇文章配图，然后投递公众号草稿")

    assert plan.steps == ("image", "deliver")
    assert plan.event_detail().startswith("计划：")
