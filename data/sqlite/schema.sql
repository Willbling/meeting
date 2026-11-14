CREATE TABLE people (
    person_id TEXT PRIMARY KEY CHECK (
        length(CAST(person_id AS BLOB)) = 7 AND person_id GLOB 'P[0-9][0-9][0-9][0-9][0-9][0-9]'
    ),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    birthday TEXT CHECK (
        birthday IS NULL OR (
            birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            AND date(birthday, '+0 days') IS birthday
        )
    ),
    phone TEXT,
    email TEXT,
    wecom_user_id TEXT UNIQUE CHECK (length(trim(wecom_user_id)) > 0)
) STRICT;

CREATE TABLE rooms (
    room_id TEXT PRIMARY KEY CHECK (
        length(CAST(room_id AS BLOB)) = 7 AND room_id GLOB 'R[0-9][0-9][0-9][0-9][0-9][0-9]'
    ),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    address TEXT NOT NULL CHECK (length(trim(address)) > 0),
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
) STRICT;

CREATE TABLE bookings (
    booking_id TEXT PRIMARY KEY CHECK (
        length(CAST(booking_id AS BLOB)) = 7 AND booking_id GLOB 'A[0-9][0-9][0-9][0-9][0-9][0-9]'
    ),
    room_id TEXT NOT NULL REFERENCES rooms (room_id),
    host_id TEXT NOT NULL REFERENCES people (person_id),
    created_by TEXT NOT NULL REFERENCES people (person_id),
    starts_at INTEGER NOT NULL,
    ends_at INTEGER NOT NULL CHECK (ends_at > starts_at),
    subject TEXT NOT NULL CHECK (length(trim(subject)) > 0),
    content TEXT,
    canceled_at INTEGER
) STRICT;

CREATE INDEX bookings_active_room_time ON bookings (room_id, starts_at, ends_at)
WHERE canceled_at IS NULL;

CREATE TABLE booking_members (
    booking_id TEXT NOT NULL REFERENCES bookings (booking_id),
    person_id TEXT NOT NULL REFERENCES people (person_id),
    PRIMARY KEY (booking_id, person_id)
) STRICT;

CREATE VIEW booking_status AS
SELECT bookings.*,
       CASE
           WHEN canceled_at IS NOT NULL THEN '取消'
           WHEN unixepoch('now') < starts_at THEN '等待'
           WHEN unixepoch('now') < ends_at THEN '会议中'
           ELSE '结束'
       END AS status
FROM bookings;

CREATE VIEW booking_attendees AS
SELECT booking_id, host_id AS person_id, '主持人' AS role FROM bookings
UNION ALL
SELECT booking_id, person_id, '成员' AS role FROM booking_members;

CREATE TRIGGER bookings_check_overlap_insert
BEFORE INSERT ON bookings
WHEN NEW.canceled_at IS NULL AND EXISTS (
    SELECT 1 FROM bookings
    WHERE room_id = NEW.room_id AND canceled_at IS NULL
      AND starts_at < NEW.ends_at AND ends_at > NEW.starts_at
)
BEGIN
    SELECT RAISE(ABORT, 'room already booked for this time');
END;

CREATE TRIGGER bookings_check_overlap_update
BEFORE UPDATE OF room_id, starts_at, ends_at, canceled_at ON bookings
WHEN NEW.canceled_at IS NULL AND EXISTS (
    SELECT 1 FROM bookings
    WHERE room_id = NEW.room_id AND canceled_at IS NULL AND booking_id <> OLD.booking_id
      AND starts_at < NEW.ends_at AND ends_at > NEW.starts_at
)
BEGIN
    SELECT RAISE(ABORT, 'room already booked for this time');
END;

CREATE TRIGGER booking_members_check_host_insert
BEFORE INSERT ON booking_members
WHEN EXISTS (
    SELECT 1 FROM bookings WHERE booking_id = NEW.booking_id AND host_id = NEW.person_id
)
BEGIN
    SELECT RAISE(ABORT, 'host is already an attendee');
END;

CREATE TRIGGER booking_members_check_host_update
BEFORE UPDATE OF booking_id, person_id ON booking_members
WHEN EXISTS (
    SELECT 1 FROM bookings WHERE booking_id = NEW.booking_id AND host_id = NEW.person_id
)
BEGIN
    SELECT RAISE(ABORT, 'host is already an attendee');
END;

CREATE TRIGGER bookings_check_host_update
BEFORE UPDATE OF host_id ON bookings
WHEN EXISTS (
    SELECT 1 FROM booking_members WHERE booking_id = OLD.booking_id AND person_id = NEW.host_id
)
BEGIN
    SELECT RAISE(ABORT, 'remove member before assigning as host');
END;
