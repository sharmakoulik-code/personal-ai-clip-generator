import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import database
from .routes.projects import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.initialize()
    yield


app = FastAPI(title="Personal Clip Generator API", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(sqlite3.Error)
async def database_error(request: Request, exc: sqlite3.Error):
    logger.exception("SQLite request failed", exc_info=exc)
    return JSONResponse(status_code=503, content={"detail": "Local database unavailable"})
