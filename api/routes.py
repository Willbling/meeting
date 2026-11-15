import sqlite3
from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Query, Request

from . import store
from .dependencies import Actor, Database, Limit, Offset, check_time_order, required_timestamp, time_parameter
from .models import (
    ApiDatetime, AvailabilityMode, Booking, BookingCreate, BookingDetail, BookingId, BookingReschedule,
    BookingStatus, ErrorResponse, Person, Room, RoomAvailability, RoomId,
)
from .store import BookingError


router = APIRouter()
booking_errors = {status: {"model": ErrorResponse} for status in (401, 403, 404)}
booking_write_errors = {**booking_errors, 409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}}


@router.get("/readyz", responses={503: {"model": ErrorResponse}})
def ready(request: Request) -> dict[str, str]:
    try:
        with closing(store.connect(request.app.state.database)) as db:
            store.check_database(db)
    except sqlite3.Error as error:
        raise BookingError(503, "DATABASE_UNAVAILABLE", "会议室数据库不可用或结构不匹配") from error
    return {"status": "ready"}

@router.get("/people", response_model=list[Person])
def people(db: Database, name: str = "", limit: Limit = 50, offset: Offset = 0) -> list[Person]:
    return store.list_people(db, name.strip(), limit, offset)

@router.get("/rooms", response_model=list[RoomAvailability | Room])
def rooms(
    db: Database, name: str = "", min_capacity: Annotated[int, Query(ge=1, le=2**63 - 1)] = 1,
    starts_at: ApiDatetime | None = None, ends_at: ApiDatetime | None = None,
    limit: Limit = 50, offset: Offset = 0,
    mode: AvailabilityMode = "full",
    min_duration_minutes: Annotated[int | None, Query(ge=1, le=10080)] = None,
) -> list[Room] | list[RoomAvailability]:
    if (starts_at is None) != (ends_at is None):
        raise BookingError(422, "INVALID_TIME_RANGE", "查询空闲会议室需同时提供开始和结束时间")
    start, end = time_parameter(starts_at), time_parameter(ends_at)
    check_time_order(start, end)
    if mode == "intervals":
        if start is None or end is None:
            raise BookingError(422, "INVALID_TIME_RANGE", "区间查房需要开始和结束时间")
        return store.list_room_intervals(db, name.strip(), min_capacity, start, end,
                                         min_duration_minutes * 60 if min_duration_minutes else 1, limit, offset)
    if min_duration_minutes is not None:
        raise BookingError(422, "INVALID_AVAILABILITY_MODE", "最短时长仅用于区间查房")
    return store.list_rooms(db, name.strip(), min_capacity, start, end, limit, offset)

@router.get("/bookings", response_model=list[Booking], responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def bookings(
    db: Database, actor: Actor, room_id: RoomId | None = None, status: BookingStatus | None = None,
    starts_at: ApiDatetime | None = None, ends_at: ApiDatetime | None = None,
    limit: Limit = 50, offset: Offset = 0,
) -> list[Booking]:
    start, end = time_parameter(starts_at), time_parameter(ends_at)
    check_time_order(start, end)
    return store.list_bookings(db, actor, room_id, status, start, end, limit, offset)

@router.get("/bookings/{booking_id}", response_model=BookingDetail, responses=booking_errors)
def booking(db: Database, actor: Actor, booking_id: BookingId) -> BookingDetail:
    return store.read_booking(db, actor, booking_id)

@router.post("/bookings", response_model=BookingDetail, status_code=201, responses=booking_write_errors)
def create_booking(db: Database, actor: Actor, body: BookingCreate) -> BookingDetail:
    return store.create_booking(db, actor, body, required_timestamp(body.starts_at), required_timestamp(body.ends_at))

@router.patch("/bookings/{booking_id}", response_model=BookingDetail, responses=booking_write_errors)
def reschedule(db: Database, actor: Actor, booking_id: BookingId, body: BookingReschedule) -> BookingDetail:
    start, end = time_parameter(body.starts_at), time_parameter(body.ends_at)
    return store.reschedule_booking(db, actor, booking_id, body.room_id, start, end, request=body)

@router.post("/bookings/{booking_id}/cancel", response_model=BookingDetail, responses=booking_write_errors)
def cancel(db: Database, actor: Actor, booking_id: BookingId) -> BookingDetail:
    return store.cancel_booking(db, actor, booking_id)
