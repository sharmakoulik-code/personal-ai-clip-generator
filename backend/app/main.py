import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import database
from .routes.projects import router
from .routes.videos import router as videos_router
from .routes.analysis import router as analysis_router
from .routes.rendering import router as rendering_router
from .services import ingestion, analysis, rendering

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.initialize()
    ingestion.start_worker()
    try:
        analysis.start_worker()
        try:
            rendering.start_worker()
        except Exception:
            analysis.stop_worker()
            raise
    except Exception:
        ingestion.stop_worker()
        raise
    try:
        yield
    finally:
        rendering.stop_worker()
        analysis.stop_worker()
        ingestion.stop_worker()


app = FastAPI(title="Personal Clip Generator API", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(videos_router)
app.include_router(analysis_router)
app.include_router(rendering_router)


@app.exception_handler(sqlite3.Error)
async def database_error(request: Request, exc: sqlite3.Error):
    logger.exception("SQLite request failed", exc_info=exc)
    return JSONResponse(status_code=503, content={"detail": "Local database unavailable"})
