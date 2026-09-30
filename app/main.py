"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import init_db_with_retry
from .errors import ApiError
from .routes import router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_with_retry()
    yield


app = FastAPI(
    title="Trail Race Timing Service",
    description=(
        "Offline trail-race timing: ordered checkpoints, idempotent scan "
        "ingestion, live standings, and immutable published result versions."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = []
    for err in exc.errors():
        err = dict(err)
        if "ctx" in err:  # ctx may hold raw exception objects
            err["ctx"] = {k: str(v) for k, v in err["ctx"].items()}
        details.append(err)
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "request validation failed",
                "details": jsonable_encoder(details),
            }
        },
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def index():
    return {
        "service": "trail-race-timing",
        "docs": "/docs",
        "health": "/health",
    }


app.include_router(router)
