from __future__ import annotations

from app.config import get_settings
from app.domain.models import RawSourceItem, SourceKind
from app.services.generator import build_generator
from app.storage.database import SessionLocal
from app.workflows.content_workflow import ContentPipeline


def main() -> None:
    sample = RawSourceItem(
        source_kind=SourceKind.GITHUB,
        external_id="demo/agent-news-workflow",
        title="Agent News Workflow",
        url="https://github.com/demo/agent-news-workflow",
        author="demo",
        summary="An example trending project for the local end-to-end review workflow.",
        content="An example trending project for the local end-to-end review workflow.",
        source_name="GitHub Trending Demo",
        metrics={"stars_total": 1200, "stars_period": 180, "forks": 80},
        metadata={"periods": {"daily": 1}},
    )
    with SessionLocal() as session:
        result = ContentPipeline(session, build_generator(get_settings())).process([sample])
    print(result)


if __name__ == "__main__":
    main()
