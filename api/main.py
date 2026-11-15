import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from data.sqlite.init_db import DEFAULT_DATABASE

from .models import ErrorResponse
from .routes import router
from .store import BookingError


def create_app(database: Path | None = None) -> FastAPI:
    app = FastAPI(title="会议室预订 API", responses={422: {"model": ErrorResponse}})
    app.state.database = database if database is not None else Path(os.getenv("MEETING_DB_PATH") or DEFAULT_DATABASE)

    @app.exception_handler(BookingError)
    async def booking_error(request: Request, error: BookingError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"code": error.code, "message": error.message})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "请求参数有误",
                "details": [{"location": item["loc"], "message": item["msg"]} for item in error.errors()],
            },
        )

    app.include_router(router)
    return app


app = create_app()
