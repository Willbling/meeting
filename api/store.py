import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from itertools import groupby
from time import time
from typing import Iterator

from .models import BEIJING, Attendee, Booking, BookingCreate, BookingDetail, BookingReschedule, Person, Room, RoomAvailability, TimeInterval


class BookingError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


BOOKING_SELECT = """
    SELECT b.*, r.name AS room_name, r.address AS room_address,
           host.name AS host_name, creator.name AS creator_name
    FROM booking_state AS b
    JOIN rooms AS r ON r.room_id = b.room_id
    JOIN people AS host ON host.person_id = b.host_id
    JOIN people AS creator ON creator.person_id = b.created_by
"""


def connect(path: Path) -> sqlite3.Connection:
    # Open an existing database and fail if its configured path is wrong.
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, autocommit=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@contextmanager
def transaction(db: sqlite3.Connection, *, write: bool = False) -> Iterator[None]:
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield
        db.execute("COMMIT")
    except BaseException as error:
        if db.in_transaction:
            db.execute("ROLLBACK")
        if isinstance(error, sqlite3.IntegrityError) and str(error) == "room already booked for this time":
            raise BookingError(409, "ROOM_TIME_CONFLICT", "会议室在该时段已被预订") from error
        if isinstance(error, sqlite3.OperationalError):
            code = getattr(error, "sqlite_errorcode", 0) & 0xff
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise BookingError(503, "DATABASE_BUSY", "数据库正被其他操作占用，请稍后重试") from error
            if code == sqlite3.SQLITE_READONLY:
                raise BookingError(503, "DATABASE_READONLY", "会议室数据库当前无法写入，请检查文件和目录权限") from error
        raise


def require_person(db: sqlite3.Connection, person_id: str) -> None:
    if db.execute("SELECT 1 FROM people WHERE person_id = ?", (person_id,)).fetchone() is None:
        raise BookingError(404, "PERSON_NOT_FOUND", f"找不到人员 {person_id}")


def check_database(db: sqlite3.Connection) -> None:
    db.execute("SELECT person_id, name, job_title, job_level FROM people LIMIT 0")
    db.execute(BOOKING_SELECT + " LIMIT 0")
    db.execute("SELECT booking_id, person_id, person_name, role FROM booking_attendees LIMIT 0")


def list_people(db: sqlite3.Connection, name: str, limit: int, offset: int) -> list[Person]:
    rows = db.execute(
        """
        SELECT person_id, name, job_title, job_level FROM people
        WHERE instr(name, ?) > 0 ORDER BY name, person_id LIMIT ? OFFSET ?
        """,
        (name, limit, offset),
    )
    return [Person(**row) for row in rows]


def list_rooms(
    db: sqlite3.Connection, name: str, min_capacity: int, start: int | None, end: int | None,
    limit: int, offset: int,
) -> list[Room]:
    rows = db.execute(
        """
        SELECT room_id, name, address, capacity FROM rooms
        WHERE is_active = 1 AND instr(name, ?) > 0 AND capacity >= ?
          AND (? IS NULL OR NOT EXISTS (
              SELECT 1 FROM bookings AS b
              WHERE b.room_id = rooms.room_id AND b.canceled_at IS NULL
                AND b.starts_at < ? AND b.ends_at > ?
          ))
        ORDER BY name, room_id LIMIT ? OFFSET ?
        """,
        (name, min_capacity, start, end, start, limit, offset),
    )
    return [Room(**row) for row in rows]


def list_room_intervals(
    db: sqlite3.Connection, name: str, capacity: int, start: int, end: int,
    minimum_seconds: int, limit: int, offset: int,
) -> list[RoomAvailability]:
    rows = db.execute(
        """
        SELECT r.room_id, r.name, r.address, r.capacity, b.starts_at, b.ends_at
        FROM rooms AS r
        LEFT JOIN bookings AS b
          ON b.room_id = r.room_id AND b.canceled_at IS NULL
         AND b.starts_at < ? AND b.ends_at > ?
        WHERE r.is_active = 1 AND instr(r.name, ?) > 0 AND r.capacity >= ?
        ORDER BY r.name, r.room_id, b.starts_at
        """,
        (end, start, name, capacity),
    )

    def interval(left: int, right: int) -> TimeInterval:
        return TimeInterval(starts_at=datetime.fromtimestamp(left, BEIJING), ends_at=datetime.fromtimestamp(right, BEIJING))

    results = []
    matched = 0
    for room, bookings in groupby(rows, key=lambda row: (row["room_id"], row["name"], row["address"], row["capacity"])):
        occupied: list[tuple[int, int]] = []
        for row in bookings:
            if row["starts_at"] is None:
                continue
            left, right = max(start, row["starts_at"]), min(end, row["ends_at"])
            if occupied and left <= occupied[-1][1]:
                occupied[-1] = (occupied[-1][0], max(occupied[-1][1], right))
            else:
                occupied.append((left, right))
        free = []
        cursor = start
        fits = False
        for left, right in [*occupied, (end, end)]:
            if left > cursor:
                free.append(interval(cursor, left))
                fits |= left - cursor >= minimum_seconds
            cursor = right
        if not fits:
            continue
        matched += 1
        if matched <= offset:
            continue
        room_id, room_name, address, seats = room
        results.append(RoomAvailability(
            room_id=room_id, name=room_name, address=address, capacity=seats,
            free_intervals=free, busy_intervals=[interval(left, right) for left, right in occupied],
        ))
        if len(results) == limit:
            break
    return results


def booking_values(row: sqlite3.Row) -> dict:
    values = dict(row)
    for column in ("starts_at", "ends_at", "canceled_at"):
        if values[column] is not None:
            values[column] = datetime.fromtimestamp(values[column], BEIJING)
    return values


def list_bookings(
    db: sqlite3.Connection, actor: str, room_id: str | None, status: str | None,
    start: int | None, end: int | None, limit: int, offset: int,
) -> list[Booking]:
    rows = db.execute(
        BOOKING_SELECT + """
        WHERE (b.created_by = ? OR b.host_id = ? OR EXISTS (
            SELECT 1 FROM booking_members AS m WHERE m.booking_id = b.booking_id AND m.person_id = ?
        ))
          AND (? IS NULL OR b.room_id = ?)
          AND (? IS NULL OR b.status = ?)
          AND (? IS NULL OR b.ends_at > ?)
          AND (? IS NULL OR b.starts_at < ?)
        ORDER BY b.starts_at, b.booking_id LIMIT ? OFFSET ?
        """,
        (actor, actor, actor, room_id, room_id, status, status, start, start, end, end, limit, offset),
    )
    return [Booking(**booking_values(row)) for row in rows]


def get_booking(db: sqlite3.Connection, booking_id: str) -> sqlite3.Row:
    row = db.execute(BOOKING_SELECT + " WHERE b.booking_id = ?", (booking_id,)).fetchone()
    if row is None:
        raise BookingError(404, "BOOKING_NOT_FOUND", "找不到该预约")
    return row


def booking_detail(db: sqlite3.Connection, row: sqlite3.Row) -> BookingDetail:
    attendees = db.execute(
        """
        SELECT a.person_id, a.person_name AS name, a.role, p.job_title, p.job_level
        FROM booking_attendees AS a JOIN people AS p USING (person_id)
        WHERE a.booking_id = ? ORDER BY a.role = '主持人' DESC, a.person_id
        """,
        (row["booking_id"],),
    )
    return BookingDetail(**booking_values(row), attendees=[Attendee(**attendee) for attendee in attendees])


def read_booking(db: sqlite3.Connection, actor: str, booking_id: str) -> BookingDetail:
    with transaction(db):
        row = get_booking(db, booking_id)
        if actor not in (row["created_by"], row["host_id"]) and db.execute(
            "SELECT 1 FROM booking_members WHERE booking_id = ? AND person_id = ?", (booking_id, actor)
        ).fetchone() is None:
            raise BookingError(403, "BOOKING_FORBIDDEN", "只有创建人、主持人和成员可以查看此预约")
        return booking_detail(db, row)


def check_room(db: sqlite3.Connection, room_id: str, attendee_count: int) -> None:
    room = db.execute("SELECT is_active, capacity FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
    if room is None:
        raise BookingError(404, "ROOM_NOT_FOUND", "找不到该会议室")
    if not room["is_active"]:
        raise BookingError(409, "ROOM_INACTIVE", "该会议室已停用")
    if attendee_count > room["capacity"]:
        raise BookingError(409, "ROOM_CAPACITY_EXCEEDED", "与会人数超过会议室容量")


def check_future_window(start: int, end: int, now: int) -> None:
    if end <= start:
        raise BookingError(422, "INVALID_TIME_RANGE", "结束时间必须晚于开始时间")
    if start <= now:
        raise BookingError(422, "START_NOT_IN_FUTURE", "开始时间必须晚于当前时间")


def create_booking(db: sqlite3.Connection, actor: str, request: BookingCreate, start: int, end: int) -> BookingDetail:
    with transaction(db, write=True):
        check_future_window(start, end, int(time()))
        check_room(db, request.room_id, 1 + len(request.member_ids))
        for person_id in (request.host_id, *request.member_ids):
            require_person(db, person_id)

        last_id = db.execute("SELECT max(booking_id) FROM bookings").fetchone()[0]
        next_id = int(last_id[1:]) + 1 if last_id else 1
        if next_id > 999999:
            raise BookingError(409, "BOOKING_ID_EXHAUSTED", "当前预约编号已用完")
        booking_id = f"A{next_id:06d}"
        db.execute(
            """
            INSERT INTO bookings (booking_id, room_id, host_id, created_by, starts_at, ends_at, subject, content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (booking_id, request.room_id, request.host_id, actor, start, end, request.subject, request.content),
        )
        db.executemany(
            "INSERT INTO booking_members (booking_id, person_id) VALUES (?, ?)",
            [(booking_id, person_id) for person_id in request.member_ids],
        )
        return booking_detail(db, get_booking(db, booking_id))


def require_editor(row: sqlite3.Row, actor: str) -> None:
    if actor not in (row["created_by"], row["host_id"]):
        raise BookingError(403, "BOOKING_FORBIDDEN", "只有创建人和主持人可以编辑或取消")


def require_waiting(row: sqlite3.Row, now: int) -> None:
    if row["canceled_at"] is not None or row["starts_at"] <= now:
        raise BookingError(409, "BOOKING_NOT_WAITING", "只有等待开始的预约可以编辑或取消")


def reschedule_booking(
    db: sqlite3.Connection, actor: str, booking_id: str, room_id: str | None,
    start: int | None, end: int | None, *, request: BookingReschedule | None = None,
) -> BookingDetail:
    with transaction(db, write=True):
        row = get_booking(db, booking_id)
        require_editor(row, actor)
        now = int(time())
        require_waiting(row, now)
        room_id = room_id or row["room_id"]
        start = row["starts_at"] if start is None else start
        end = row["ends_at"] if end is None else end
        check_future_window(start, end, now)
        members = request.member_ids if request else None
        if members is None:
            member_count = db.execute(
                "SELECT count(*) FROM booking_members WHERE booking_id = ?", (booking_id,)
            ).fetchone()[0]
        else:
            if row["host_id"] in members:
                raise BookingError(422, "HOST_IN_MEMBERS", "其他与会人员名单不能包含主持人")
            for person_id in members:
                require_person(db, person_id)
            member_count = len(members)
        check_room(db, room_id, 1 + member_count)
        subject = request.subject if request and request.subject is not None else row["subject"]
        content = request.content if request and "content" in request.model_fields_set else row["content"]
        db.execute(
            "UPDATE bookings SET room_id = ?, starts_at = ?, ends_at = ?, subject = ?, content = ? WHERE booking_id = ?",
            (room_id, start, end, subject, content, booking_id),
        )
        if members is not None:
            db.execute("DELETE FROM booking_members WHERE booking_id = ?", (booking_id,))
            db.executemany("INSERT INTO booking_members VALUES (?, ?)", [(booking_id, person_id) for person_id in members])
        return booking_detail(db, get_booking(db, booking_id))


def cancel_booking(db: sqlite3.Connection, actor: str, booking_id: str) -> BookingDetail:
    with transaction(db, write=True):
        row = get_booking(db, booking_id)
        require_editor(row, actor)
        if row["canceled_at"] is None:
            now = int(time())
            require_waiting(row, now)
            db.execute("UPDATE bookings SET canceled_at = ? WHERE booking_id = ?", (now, booking_id))
            row = get_booking(db, booking_id)
        return booking_detail(db, row)
