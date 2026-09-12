"""在同步上下文里执行协程。

用途：会话 Agent（DeepAgent）以**同步方式**执行工具；如果工具只实现 `async def` 而没有同步入口，
LangChain 会抛 `NotImplementedError`（真实故障：对话模型报“暂时不可用”，日志里
`failure_stage=agent_invoke error_type=NotImplementedError`）。因此这类工具必须提供同步外壳。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor


def run_coroutine_sync(coro):
    """执行协程；调用方已经在事件循环里时改在一次性线程中运行它自己的循环。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
