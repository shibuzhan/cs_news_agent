from __future__ import annotations

import logging
import os

from app.config import Settings
from app.observability import LOG_FORMAT, configure_langsmith, configure_logging


def _remove_project_log_handlers() -> None:
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, "_news_agent_handler", False):
            root_logger.removeHandler(handler)
            handler.close()


def test_file_logging_uses_the_project_formatter(tmp_path) -> None:
    _remove_project_log_handlers()
    log_file = tmp_path / "agent.log"
    configure_logging(Settings(log_file=str(log_file)))
    logging.getLogger("news_agent.test").info("safe_probe request_id=run-1")
    _remove_project_log_handlers()

    line = log_file.read_text(encoding="utf-8").strip()
    assert " - news_agent.test - INFO - safe_probe request_id=run-1" in line
    assert logging.Formatter(LOG_FORMAT)


def test_langsmith_environment_remains_disabled_without_a_key(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    settings = Settings(langsmith_tracing=True, langsmith_api_key=None)

    configure_langsmith(settings)

    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_langsmith_environment_is_configured_only_when_enabled(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_WORKSPACE_ID", raising=False)
    settings = Settings(
        langsmith_tracing=True,
        langsmith_api_key="test-key",
        langsmith_project="test-project",
        langsmith_endpoint="https://example.test",
    )

    configure_langsmith(settings)

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_PROJECT"] == "test-project"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://example.test"
