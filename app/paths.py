"""运行期项目资源目录解析。

容器内以控制台脚本启动时（`uvicorn` / `arq`），当前工作目录不在 `sys.path` 首位，
`import app` 可能命中 site-packages 中的**非可编辑副本**（Dockerfile 使用
`pip install --no-deps .`）。此时按 `__file__` 推导出的“仓库根”实际是 site-packages，
其中并不包含 `agent_skills/`，来源写作 Skill 与微信 Skill 脚本会静默降级。

这里按固定顺序探测可用的资源根目录，并把最终选择写入日志，避免再次静默失效。
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger("news_agent.paths")

# 显式覆盖入口，便于非常规部署（例如资源与代码分离挂载）。
ASSET_DIR_ENV = "NEWS_AGENT_ASSET_DIR"
ASSET_DIR_NAME = "agent_skills"
_FALLBACK_ASSET_ROOTS = (Path("/app"),)


def _candidate_roots() -> list[Path]:
    """按优先级给出候选资源根目录（可能不存在，逐个探测）。"""
    roots: list[Path] = []
    override = os.environ.get(ASSET_DIR_ENV)
    if override:
        roots.append(Path(override))
    roots.append(Path(__file__).resolve().parents[1])  # 包旁：仓库根或 site-packages
    roots.append(Path.cwd())  # 容器 WORKDIR /app，本地为项目根
    roots.extend(_FALLBACK_ASSET_ROOTS)
    return roots


@lru_cache(maxsize=4)
def _asset_root() -> Path | None:
    for root in _candidate_roots():
        try:
            if (root / ASSET_DIR_NAME).is_dir():
                logger.info("asset_root_resolved root=%s", root)
                return root
        except OSError:  # 不可访问的候选目录直接跳过
            continue
    logger.warning(
        "asset_root_unresolved checked=%s", [str(item) for item in _candidate_roots()]
    )
    return None


def agent_skills_dir() -> Path | None:
    """返回可用的 `agent_skills/` 目录；全部候选都缺失时返回 None。"""
    root = _asset_root()
    return (root / ASSET_DIR_NAME) if root is not None else None


def resolve_asset(relative_path: str) -> Path | None:
    """解析 `agent_skills/` 下的资源文件；不存在时返回 None，由调用方保持既有降级行为。"""
    skills = agent_skills_dir()
    if skills is None:
        return None
    candidate = skills / relative_path
    return candidate if candidate.exists() else None
