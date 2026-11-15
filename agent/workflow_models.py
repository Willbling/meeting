from dataclasses import dataclass, field
from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.models import ApiDatetime, BookingId, BookingStatus, RoomId


type Intent = Literal["rooms", "bookings", "detail", "create", "reschedule", "cancel"]


LABELS = {"create": "预订", "reschedule": "改期", "cancel": "取消"}
PAGE_SIZE = 5


@dataclass(frozen=True)
class WorkflowIssue:
    reason: str
    needed: tuple[str, ...]
    suggestion: str


@dataclass(frozen=True)
class MeetingQuestion:
    text: str
    fields: tuple[str, ...]


@dataclass
class BookingSearch:
    scope: dict
    items: list[dict]
    complete: bool

    def context(self) -> dict:
        return {"scope": self.scope, "candidates": [booking_reference(item) for item in self.items],
                "complete": self.complete}


def booking_reference(item: dict) -> dict:
    return {key: item[key] for key in ("booking_id", "subject", "starts_at", "ends_at", "room_name", "status")}


class Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    intent: Literal[
        "rooms", "bookings", "detail", "create", "reschedule", "cancel", "continue",
        "confirm", "booking_status", "help", "unknown",
    ]
    starts_at: ApiDatetime | None = None
    ends_at: ApiDatetime | None = None
    start_clock: time | None = None
    end_clock: time | None = None
    target_start: ApiDatetime | None = None
    target_end: ApiDatetime | None = None
    booking_id: BookingId | None = None
    target_subject: str | None = None
    target_date_hint: str | None = None
    use_booking_search: bool = False
    status: BookingStatus | None = None
    subject: str | None = None
    content: str | None = None
    host: str | None = None
    members: list[str] | None = None
    capacity: int | None = Field(default=None, ge=1, le=2**63 - 1)
    duration_minutes: int | None = Field(default=None, ge=1, le=10080)
    day_part: Literal["morning", "afternoon", "evening"] | None = None
    flexible: bool | None = None
    attendee_count: int | None = Field(default=None, ge=1, le=10000)
    member_mode: Literal["replace", "add", "remove"] | None = None
    clear_content: bool | None = None
    recent_booking: bool = False
    room_id: RoomId | None = None
    room_name: str | None = None
    any_room: bool | None = None
    selection: int | None = Field(default=None, ge=1, le=PAGE_SIZE)
    date_hint: str | None = None
    date_reference: str | None = None
    keep_original_time: bool | None = None
    room_info: bool = False


@dataclass
class DraftUserMessage:
    text: str
    at: str


@dataclass
class Draft:
    intent: Intent
    fields: dict = field(default_factory=dict)
    choices: list[dict] = field(default_factory=list)
    choice_kind: str = ""
    resolved_people: dict[str, dict] = field(default_factory=dict)
    attendee_source: str | None = None
    user_messages: list[DraftUserMessage] = field(default_factory=list)

    def remember_user_message(self, message: DraftUserMessage) -> None:
        if self.user_messages and self.user_messages[-1] == message:
            return
        self.user_messages.append(message)
        # Keep the request that opened the draft and the latest seven updates.
        if len(self.user_messages) > 8:
            self.user_messages[1:-7] = []


@dataclass
class Application:
    intent: Intent
    arguments: dict
    summary: str
    original: dict | None = None
    known_ids: frozenset[str] = frozenset()


@dataclass
class WorkflowMemory:
    draft: Draft | None = None
    query: Draft | None = None
    pending: Application | None = None
    uncertain: Application | None = None
    last_booking: str | None = None
    last_written_booking: str | None = None
    booking_search: BookingSearch | None = None
    question: MeetingQuestion | None = None

    def step(self) -> str | None:
        if self.uncertain:
            return "待核实提交结果"
        if self.pending:
            return f"{LABELS[self.pending.intent]}待确认"
        if self.draft:
            if self.draft.choice_kind == "room":
                return f"{LABELS[self.draft.intent]}待选会议室"
            return f"{LABELS[self.draft.intent]}草稿待补充"
        return "有近期提交记录" if self.last_written_booking else None
