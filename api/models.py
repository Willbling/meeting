from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from pydantic.functional_validators import BeforeValidator


BEIJING = ZoneInfo("Asia/Shanghai")
PersonId = Annotated[str, StringConstraints(pattern=r"^P[0-9]{6}$", min_length=7, max_length=7)]
RoomId = Annotated[str, StringConstraints(pattern=r"^R[0-9]{6}$", min_length=7, max_length=7)]
BookingId = Annotated[str, StringConstraints(pattern=r"^A[0-9]{6}$", min_length=7, max_length=7)]
Subject = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
BookingStatus = Literal["等待", "会议中", "取消", "结束"]


def require_iso_datetime(value: object) -> object:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Use an ISO 8601 date and time with a UTC offset")
    return value


ApiDatetime = Annotated[AwareDatetime, BeforeValidator(require_iso_datetime)]


def timestamp(value: datetime) -> int:
    if value.microsecond:
        raise ValueError("Time must use whole seconds")
    value.astimezone(BEIJING)
    return int(value.timestamp())


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationDetail(BaseModel):
    location: list[str | int]
    message: str


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: list[ValidationDetail] | None = None


class Person(BaseModel):
    person_id: PersonId
    name: str


class Room(BaseModel):
    room_id: RoomId
    name: str
    address: str
    capacity: int


class Attendee(Person):
    role: Literal["主持人", "成员"]


class Booking(BaseModel):
    booking_id: BookingId
    room_id: RoomId
    host_id: PersonId
    created_by: PersonId
    starts_at: datetime
    ends_at: datetime
    subject: str
    status: BookingStatus
    canceled_at: datetime | None


class BookingDetail(Booking):
    content: str | None
    attendees: list[Attendee]


class BookingCreate(RequestBody):
    room_id: RoomId
    host_id: PersonId
    starts_at: ApiDatetime
    ends_at: ApiDatetime
    subject: Subject
    content: str | None = None
    member_ids: list[PersonId] = Field(default_factory=list)

    @field_validator("member_ids")
    @classmethod
    def unique_members(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("member_ids must be unique")
        return value

    @model_validator(mode="after")
    def separate_host(self) -> "BookingCreate":
        if self.host_id in self.member_ids:
            raise ValueError("host_id must not also appear in member_ids")
        return self


class BookingReschedule(RequestBody):
    room_id: RoomId | None = None
    starts_at: ApiDatetime | None = None
    ends_at: ApiDatetime | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "BookingReschedule":
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Provide at least one non-null field to change")
        return self
