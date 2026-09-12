from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.conversation_agent import ConversationAgent
from app.domain.models import (
    ConversationDecision,
    ConversationIntent,
    ConversationRunStatus,
)
from app.services.conversation import DeterministicConversationModel, OpenAICompatibleConversationModel


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.finished: dict | None = None
        self.messages = [SimpleNamespace(id="request-1", role="user", content="测试")]

    def list_chat_messages(self, _session_id: str):
        return self.messages

    def create_chat_agent_run(self, _session_id: str, _message_id: str, intent):
        return SimpleNamespace(id="run-1", intent=intent.value)

    def add_chat_agent_event(self, _run_id: str, title: str, detail: str = "", *_args, **_kwargs):
        self.events.append({"sequence": len(self.events) + 1, "title": title, "detail": detail})

    def create_chat_message(self, _session_id: str, role: str, content: str):
        message = SimpleNamespace(id=f"message-{len(self.messages)}", role=role, content=content)
        self.messages.append(message)
        return message

    def finish_chat_agent_run(self, _run_id: str, response_id: str, status, summary: str, tool_results, *_args):
        self.finished = {"response_id": response_id, "status": status, "summary": summary, "tool_results": tool_results}

    def create_schedule_plan(self, **_kwargs):
        return SimpleNamespace(id="schedule-plan-1")

    def create_publish_plan(self, **_kwargs):
        return SimpleNamespace(id="publish-plan-1")


class FixedModel:
    def __init__(self, decision: ConversationDecision):
        self.decision = decision

    def decide(
        self, _content: str, _has_attachment: bool, _history: list[dict[str, str]]
    ):
        return self.decision


class CountingModel(FixedModel):
    def __init__(self, decision: ConversationDecision):
        super().__init__(decision)
        self.calls = 0

    def decide(
        self, _content: str, _has_attachment: bool, _history: list[dict[str, str]]
    ):
        self.calls += 1
        return self.decision


@pytest.mark.asyncio
async def test_general_conversation_persists_a_safe_execution_summary() -> None:
    repository = FakeRepository()

    async def collector(_sources: list[str], _limit: int):
        raise AssertionError("通用对话不应调用采集 Tool")

    outcome = await ConversationAgent(
        repository,
        FixedModel(ConversationDecision(intent=ConversationIntent.GENERAL_CHAT, reply="你好")),
        collector,
    ).run("session-1", "request-1", "你好", False)

    assert outcome.reply == "你好"
    assert outcome.status == ConversationRunStatus.COMPLETED
    assert repository.events[0]["title"] == "识别对话意图"
    assert repository.events[1]["detail"] == "未调用外部业务 Tool"
    assert [event["sequence"] for event in repository.events] == [1, 2]


@pytest.mark.asyncio
async def test_schedule_request_only_creates_a_pending_plan() -> None:
    repository = FakeRepository()

    async def collector(_sources: list[str], _limit: int):
        raise AssertionError("定时计划不应调用采集 Tool")

    outcome = await ConversationAgent(
        repository,
        FixedModel(
            ConversationDecision(
                intent=ConversationIntent.CREATE_SCHEDULE_PLAN,
                reply="正在创建计划",
                schedule_text="每天 09:00 采集 AI 资讯",
            )
        ),
        collector,
    ).run("session-1", "request-1", "每天 9 点采集", False)

    assert outcome.status == ConversationRunStatus.WAITING_CONFIRMATION
    assert outcome.tool_results == [{"tool": "create_schedule_plan", "plan_id": "schedule-plan-1"}]
    assert "不会注册或执行任务" in outcome.reply


@pytest.mark.asyncio
async def test_publish_request_only_creates_a_pending_plan() -> None:
    repository = FakeRepository()

    async def collector(_sources: list[str], _limit: int):
        raise AssertionError("发布计划不应调用采集 Tool")

    outcome = await ConversationAgent(
        repository,
        FixedModel(ConversationDecision(intent=ConversationIntent.CREATE_PUBLISH_PLAN, reply="正在创建计划")),
        collector,
    ).run("session-1", "request-1", "发布这篇内容", False)

    assert outcome.status == ConversationRunStatus.WAITING_CONFIRMATION
    assert outcome.tool_results == [{"tool": "create_publish_plan", "plan_id": "publish-plan-1"}]
    assert "不会连接账号或发布" in outcome.reply


def test_safe_fallback_never_uses_keywords_to_create_a_plan() -> None:
    decision = DeterministicConversationModel().decide("发布这篇内容到小红书", False, [])

    assert decision.intent == ConversationIntent.GENERAL_CHAT
    assert "模型暂时不可用" in decision.reply


@pytest.mark.asyncio
async def test_precomputed_decision_prevents_a_second_intent_model_call() -> None:
    repository = FakeRepository()
    model = CountingModel(
        ConversationDecision(intent=ConversationIntent.GENERAL_CHAT, reply="你好")
    )

    async def collector(_sources: list[str], _limit: int):
        raise AssertionError("通用对话不应调用采集 Tool")

    outcome = await ConversationAgent(repository, model, collector).run(
        "session-1",
        "request-1",
        "你好",
        False,
        decision=ConversationDecision(intent=ConversationIntent.GENERAL_CHAT, reply="你好"),
    )

    assert outcome.reply == "你好"
    assert model.calls == 0


def test_safe_fallback_never_uses_source_keywords_to_call_collection() -> None:
    decision = DeterministicConversationModel().decide(
        "提取 Archive 内容编写文案", False, []
    )

    assert decision.intent == ConversationIntent.GENERAL_CHAT


def test_safe_fallback_never_mistakes_negated_image_request_for_an_image_tool() -> None:
    decision = DeterministicConversationModel().decide(
        "修改当前文章，不要再生成插图", False, []
    )

    assert decision.intent == ConversationIntent.GENERAL_CHAT


def test_llm_intent_response_controls_negated_image_request(monkeypatch) -> None:
    class FakeCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='''{
                    "intent": "general_chat",
                    "reply": "我不会生成插图。请说明希望如何修改正文。",
                    "auto_illustration": false
                }'''))]
            )

    class FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.services.conversation.OpenAI", lambda **_kwargs: FakeClient())
    settings = SimpleNamespace(
        openai_api_key="test-key", openai_base_url="https://example.invalid/v1",
        llm_model="test-model", conversation_agent_timeout_seconds=45,
        langsmith_tracing=False, langsmith_api_key=None,
    )

    decision = OpenAICompatibleConversationModel(settings).decide(
        "修改当前文章，不要再生成插图", False, []
    )

    assert decision.intent == ConversationIntent.GENERAL_CHAT
    assert decision.auto_illustration is False
