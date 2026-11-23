from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.responses import FileResponse, HTMLResponse

from app.api.routes import router
from app.config import get_settings
from app.deps import get_container
from app.quota.sweeper import run_quota_sweeper

_STATIC_DIR = Path(__file__).resolve().parent / "static"

logger = logging.getLogger(__name__)
settings = get_settings()
Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
Path(settings.data_dir, "logs").mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    c = get_container()
    pool = AsyncConnectionPool(
        conninfo=c.settings.database_url,
        min_size=1,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(conn=pool)
    await checkpointer.setup()
    c.bind_checkpointer(checkpointer)
    logger.info("PostgresSaver checkpoint tables ready")

    sweeper_task = asyncio.create_task(run_quota_sweeper(c.quota, interval_sec=5.0))
    try:
        yield
    finally:
        sweeper_task.cancel()
        try:
            await sweeper_task
        except asyncio.CancelledError:
            pass
        await pool.close()


# docs/redoc 默认走 jsdelivr，国内常超时导致 /docs 空白；改用 unpkg
app = FastAPI(
    title="Travel QA Service",
    version="0.1.0",
    description="P0 orchestrator skeleton",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/chat", include_in_schema=False)
def chat_preview():
    """Docker 内简易会话窗：走 /v1/chat/stream，便于看纯文本流式效果。"""
    path = _STATIC_DIR / "chat.html"
    if not path.is_file():
        return HTMLResponse("<p>chat UI missing</p>", status_code=404)
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/docs", include_in_schema=False)
def swagger_ui() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        swagger_js_url="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css",
    )


@app.get("/redoc", include_in_schema=False)
def redoc_ui() -> HTMLResponse:
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - ReDoc",
        redoc_js_url="https://unpkg.com/redoc@2/bundles/redoc.standalone.js",
    )


@app.get("/")
def root():
    return {
        "service": "airline_travel_agent",
        "phase": "P0",
        "docs": "/docs",
        "chat": "/chat",
        "chat_stream": "/v1/chat/stream",
        "contracts": "contracts/v0.1",
        "stm": "langgraph.AsyncPostgresSaver",
        "architecture": "see sibling repo travel-qa-agent/docs/architecture.md",
    }
