from __future__ import annotations

import logging
import os
from pathlib import Path

from app.config import Settings


LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def configure_observability(settings: Settings) -> None:
    """为 app 与 worker 配置一致的本地日志及可选 LangSmith 环境。"""
    configure_logging(settings)
    configure_langsmith(settings)


def configure_logging(settings: Settings) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT)

    for handler in root_logger.handlers:
        if getattr(handler, "_news_agent_handler", False):
            return

    log_path = Path(settings.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    stream_handler = logging.StreamHandler()
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        handler._news_agent_handler = True  # type: ignore[attr-defined]
        root_logger.addHandler(handler)

    logging.getLogger(__name__).info("logging_configured log_file=%s", log_path)


def configure_langsmith(settings: Settings) -> None:
    """仅在显式启用并配置 Key 时向进程环境注入追踪设置。"""
    if not settings.langsmith_tracing or not settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "false"
        logging.getLogger(__name__).info("langsmith_tracing_disabled")
        return

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    if settings.langsmith_workspace_id:
        os.environ["LANGSMITH_WORKSPACE_ID"] = settings.langsmith_workspace_id
    logging.getLogger(__name__).info(
        "langsmith_tracing_enabled project=%s endpoint=%s",
        settings.langsmith_project,
        settings.langsmith_endpoint,
    )


def langsmith_enabled(settings: Settings) -> bool:
    return settings.langsmith_tracing and bool(settings.langsmith_api_key)
