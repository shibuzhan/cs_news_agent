from __future__ import annotations

import logging
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.observability import configure_observability
from app.storage.repositories import (
    AttachmentNotFound,
    ChatSessionNotFound,
    DraftNotFound,
    InvalidReviewTransition,
    PublicationAssetNotFound,
    WechatPublicationNotFound,
)

settings = get_settings()
configure_observability(settings)
logger = logging.getLogger("news_agent.api")

app = FastAPI(title="资讯运营 Agent", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request_failed request_id=%s path=%s", request_id, request.url.path)
        raise
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_completed request_id=%s method=%s path=%s status=%s elapsed_ms=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.exception_handler(DraftNotFound)
async def draft_not_found(_request: Request, exc: DraftNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(InvalidReviewTransition)
async def invalid_transition(_request: Request, exc: InvalidReviewTransition):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ChatSessionNotFound)
async def chat_session_not_found(_request: Request, exc: ChatSessionNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(AttachmentNotFound)
async def attachment_not_found(_request: Request, exc: AttachmentNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(PublicationAssetNotFound)
async def publication_asset_not_found(_request: Request, exc: PublicationAssetNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(WechatPublicationNotFound)
async def wechat_publication_not_found(_request: Request, exc: WechatPublicationNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/")
def root() -> dict[str, str]:
    return {"name": settings.app_name, "docs": "/docs", "health": "/api/health"}


app.include_router(router, prefix="/api")
