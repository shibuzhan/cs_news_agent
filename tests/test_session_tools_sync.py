"""回归：会话 Agent 注册的工具必须能被**同步**调用。

真实故障：把工具写成只实现 `async def` 而没有同步入口时，DeepAgent 以同步方式执行会抛
`NotImplementedError`，用户侧表现为“对话模型暂时不可用”，与模型可用性无关、极难排查。
"""

from __future__ import annotations

from app.agent_tools.conversation_context import build_conversation_context_tools
from app.agent_tools.draft_actions import build_draft_action_tools
from app.agent_tools.draft_assets import build_draft_asset_tools
from app.agent_tools.source_media_tools import build_source_media_tools


def _all_session_tools(session_id: str = "session-1"):
    return [
        *build_conversation_context_tools(session_id),
        *build_draft_asset_tools(session_id),
        *build_draft_action_tools(session_id),
        *build_source_media_tools(session_id),
    ]


def test_every_session_tool_has_a_sync_entrypoint() -> None:
    tools = _all_session_tools()

    assert tools, "会话 Agent 至少要注册一个工具"
    async_only = [tool.name for tool in tools if getattr(tool, "func", None) is None]
    assert async_only == [], f"这些工具只有异步实现，DeepAgent 同步执行会抛 NotImplementedError：{async_only}"


def test_async_bridge_runs_coroutines_from_sync_context() -> None:
    import asyncio

    from app.services.async_bridge import run_coroutine_sync

    async def work(value: int) -> int:
        await asyncio.sleep(0)
        return value * 2

    assert run_coroutine_sync(work(21)) == 42


def test_async_bridge_works_inside_a_running_loop() -> None:
    import asyncio

    from app.services.async_bridge import run_coroutine_sync

    async def work() -> str:
        await asyncio.sleep(0)
        return "ok"

    async def caller() -> str:
        # 调用方已经在事件循环里：桥接必须改用一次性线程而不是 asyncio.run。
        return run_coroutine_sync(work())

    assert asyncio.run(caller()) == "ok"
