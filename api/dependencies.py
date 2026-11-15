import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Annotated, Iterator

from fastapi import Depends, Header, Query, Request

from . import store
from .models import PersonId, timestamp
from .store import BookingError


Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=2**63 - 1)]


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(request.app.state.database)) as db:
        yield db


Database = Annotated[sqlite3.Connection, Depends(get_db)]


def get_actor(db: Database, actor: Annotated[PersonId | None, Header(alias="X-Person-ID")] = None) -> str:
    if actor is None:
        raise BookingError(401, "ACTOR_REQUIRED", "请通过 X-Person-ID 指定操作者身份")
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
