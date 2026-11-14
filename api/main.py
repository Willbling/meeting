import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import store
from .models import (
    ApiDatetime, Booking, BookingCreate, BookingDetail, BookingId, BookingReschedule,
    BookingStatus, ErrorResponse, Person, PersonId, Room, RoomId, timestamp,
)
from .store import BookingError


DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "data" / "sqlite" / "meeting.db"
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=2**63 - 1)]


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(request.app.state.database)) as db:
        yield db


Database = Annotated[sqlite3.Connection, Depends(get_db)]


def get_actor(db: Database, actor: Annotated[PersonId | None, Header(alias="X-Person-ID")] = None) -> str:
    if actor is None:
        raise BookingError(401, "ACTOR_REQUIRED", "请通过 X-Person-ID 指定本地测试身份")
    store.require_person(db, actor)
    return actor


Actor = Annotated[str, Depends(get_actor)]


def required_timestamp(value: datetime) -> int:
    try:
        return timestamp(value)
    except (ValueError, OverflowError, OSError) as error:
        raise BookingError(422, "INVALID_DATETIME", "请输入可转换为北京时间的整秒时间") from error


def time_parameter(value: datetime | None) -> int | None:
    return None if value is None else required_timestamp(value)


def check_time_order(start_at: int | None, end_at: int | None) -> None:
    if start_at is not None and end_at is not None and end_at <= start_at:
        raise BookingError(422, "INVALID_TIME_RANGE", "结束时间必须晚于开始时间")


def create_app(database: Path | None = None) -> FastAPI:
    app = FastAPI(title="会议室预订 API", responses={422: {"model": ErrorResponse}})
    app.state.database = database if database is not None else Path(os.getenv("MEETING_DB_PATH") or DEFAULT_DATABASE)
    booking_errors = {status: {"model": ErrorResponse} for status in (401, 403, 404)}
    booking_write_errors = {**booking_errors, 409: {"model": ErrorResponse}}

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

    @app.get("/people", response_model=list[Person])
    def people(db: Database, name: str = "", limit: Limit = 50, offset: Offset = 0) -> list[Person]:
        return store.list_people(db, name.strip(), limit, offset)

    @app.get("/rooms", response_model=list[Room])
    def rooms(
        db: Database, name: str = "", min_capacity: Annotated[int, Query(ge=1, le=2**63 - 1)] = 1,
        starts_at: ApiDatetime | None = None, ends_at: ApiDatetime | None = None,
        limit: Limit = 50, offset: Offset = 0,
    ) -> list[Room]:
        if (starts_at is None) != (ends_at is None):
            raise BookingError(422, "INVALID_TIME_RANGE", "查询空闲会议室需同时提供开始和结束时间")
        start, end = time_parameter(starts_at), time_parameter(ends_at)
        check_time_order(start, end)
        return store.list_rooms(db, name.strip(), min_capacity, start, end, limit, offset)

    @app.get("/bookings", response_model=list[Booking], responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
    def bookings(
        db: Database, actor: Actor, room_id: RoomId | None = None, status: BookingStatus | None = None,
        starts_at: ApiDatetime | None = None, ends_at: ApiDatetime | None = None,
        limit: Limit = 50, offset: Offset = 0,
    ) -> list[Booking]:
        start, end = time_parameter(starts_at), time_parameter(ends_at)
        check_time_order(start, end)
        return store.list_bookings(db, actor, room_id, status, start, end, limit, offset)

    @app.get("/bookings/{booking_id}", response_model=BookingDetail, responses=booking_errors)
    def booking(db: Database, actor: Actor, booking_id: BookingId) -> BookingDetail:
        return store.read_booking(db, actor, booking_id)

    @app.post("/bookings", response_model=BookingDetail, status_code=201, responses=booking_write_errors)
    def create_booking(db: Database, actor: Actor, body: BookingCreate) -> BookingDetail:
        return store.create_booking(db, actor, body, required_timestamp(body.starts_at), required_timestamp(body.ends_at))

    @app.patch("/bookings/{booking_id}", response_model=BookingDetail, responses=booking_write_errors)
    def reschedule(db: Database, actor: Actor, booking_id: BookingId, body: BookingReschedule) -> BookingDetail:
        start, end = time_parameter(body.starts_at), time_parameter(body.ends_at)
        return store.reschedule_booking(db, actor, booking_id, body.room_id, start, end)

    @app.post("/bookings/{booking_id}/cancel", response_model=BookingDetail, responses=booking_write_errors)
    def cancel(db: Database, actor: Actor, booking_id: BookingId) -> BookingDetail:
        return store.cancel_booking(db, actor, booking_id)

    return app


app = create_app()
