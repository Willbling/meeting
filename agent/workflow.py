import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.models import BEIJING, ApiDatetime, BookingId, BookingStatus, RoomId


type Intent = Literal["rooms", "bookings", "detail", "create", "reschedule", "cancel"]
WRITES = {"create": "create_booking", "reschedule": "reschedule_booking", "cancel": "cancel_booking"}
LABELS = {"create": "预订", "reschedule": "改期", "cancel": "取消"}
UNCERTAIN = {"API_TIMEOUT", "API_UNAVAILABLE", "API_INVALID_RESPONSE", "API_REQUEST_FAILED", "MCP_CALL_FAILED"}
PAGE_SIZE = 5
RELATIVE_DAYS = {"今天": 0, "明天": 1, "后天": 2, "昨天": -1, "前天": -2}
DAY_PARTS = {"morning": ("08:00", "12:00"), "afternoon": ("13:00", "18:00"), "evening": ("18:00", "22:00")}


def whole_day(hint: str, today: date) -> dict[str, str]:
    try:
        day = today + timedelta(days=RELATIVE_DAYS[hint]) if hint in RELATIVE_DAYS else date.fromisoformat(hint)
        start = datetime.combine(day, time.min, BEIJING)
        end = start + timedelta(days=1)
    except (ValueError, OverflowError):
        return {}
    return {"starts_at": start.isoformat(timespec="seconds"), "ends_at": end.isoformat(timespec="seconds")}


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


def subject_matches(subject: str, clue: str) -> bool:
    if clue.casefold() in subject.casefold():
        return True
    words = [word for word in re.split(r"[的\s、，]+", clue.casefold()) if word]
    return len(words) > 1 and all(len(word) >= 2 and word in subject.casefold() for word in words)


class Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    intent: Literal[
        "rooms", "bookings", "detail", "create", "reschedule", "cancel", "continue",
        "confirm", "help", "unknown",
    ]
    starts_at: ApiDatetime | None = None
    ends_at: ApiDatetime | None = None
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
    keep_original_time: bool | None = None
    room_info: bool = False


@dataclass
class Draft:
    intent: Intent
    fields: dict = field(default_factory=dict)
    choices: list[dict] = field(default_factory=list)
    choice_kind: str = ""
    resolved_people: dict[str, dict] = field(default_factory=dict)
    attendee_source: str | None = None


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


def window(item: dict) -> str:
    start, end = (datetime.fromisoformat(item[key]).astimezone(BEIJING) for key in ("starts_at", "ends_at"))
    end_format = "%H:%M" if start.date() == end.date() else "%m-%d %H:%M"
    return f"{start:%m-%d %H:%M}–{end:{end_format}}（北京时间）"


def booking_line(item: dict) -> str:
    return f"「{item['subject']}」 {window(item)}，{item['room_name']}（{item['room_address']}），申请编号 {item['booking_id']}"


class ToolFailure(Exception):
    def __init__(self, result: dict) -> None:
        self.result = result
        super().__init__(result.get("message") or "工具没有返回可用结果。")


class BookingWorkflow(WorkflowMemory):
    def __init__(self, call_tool: Callable[[str, dict], Awaitable[dict]], person_id: str) -> None:
        super().__init__()
        self.call_tool = call_tool
        self.person_id = person_id
        self.issue: WorkflowIssue | None = None
        self.before_write: Callable[[], Awaitable[None]] | None = None

    def restore(self, memory: WorkflowMemory) -> None:
        self.__dict__.update(vars(memory))

    def snapshot(self) -> WorkflowMemory:
        return WorkflowMemory(**{key: getattr(self, key) for key in WorkflowMemory.__dataclass_fields__})

    def needs(self, reason: str, needed: tuple[str, ...], suggestion: str, fallback: str) -> str:
        self.issue = WorkflowIssue(reason, needed, suggestion)
        return fallback

    def context(self) -> dict:
        def compact(draft: Draft | None) -> dict | None:
            if draft is None:
                return None
            choices = [{key: item[key] for key in (
                "booking_id", "name", "job_title", "job_level", "subject", "starts_at", "room_name",
            ) if key in item} for item in draft.choices]
            return {"intent": draft.intent, "fields": draft.fields, "choice_kind": draft.choice_kind,
                    "choices": choices, "attendee_source": draft.attendee_source}

        return {"draft": compact(self.draft), "query": compact(self.query),
                "waiting_for_confirmation": self.pending.intent if self.pending else None,
                "last_booking": self.last_booking, "last_written_booking": self.last_written_booking,
                "booking_search": self.booking_search.context() if self.booking_search else None}

    async def call(self, tool: str, **params) -> dict:
        result = await self.call_tool(tool, {key: value for key, value in params.items() if value is not None})
        if result.get("is_error"):
            raise ToolFailure(result)
        return result

    @staticmethod
    def choose(draft: Draft, kind: str, items: list[dict], formatter: Callable[[dict], str], question: str) -> str:
        draft.choice_kind = kind
        draft.choices = items[:PAGE_SIZE]
        choices = "\n".join(f"{index}. {formatter(item)}" for index, item in enumerate(draft.choices, 1))
        more = "\n还有其他候选，可以补充条件缩小范围。" if len(items) > PAGE_SIZE else ""
        return f"{question}\n{choices}{more}"

    async def apply(self, meaning: Interpretation, *, resume_write: bool = False, attendee_source: str | None = None) -> str:
        intent = meaning.intent
        if intent == "help":
            return "可以查空闲会议室、查我的预约，也可以帮你预订、改期或取消。比如「明天 14:00 到 15:00，查能坐 8 人的会议室」。"
        if intent in {"unknown", "confirm"}:
            return self.needs("intent_unclear", ("intent",), "核对用户是否在查自己的安排、查空房，或要修改预约。",
                              "我还没确定你想查安排、查空房，还是办理预约。")
        if intent == "continue":
            draft = self.draft if resume_write else self.query or self.draft
            if draft is None:
                return "目前没有等待补充的请求。你想查会议室、查预约，还是办理预订？"
            intent = draft.intent
        elif intent in WRITES:
            self.pending = None
            if self.draft is None or self.draft.intent != intent:
                self.draft = Draft(intent)
                if intent == "create" and self.query and self.query.intent == "rooms":
                    search = self.query.fields
                    self.draft.fields.update({key: search[key] for key in ("date_hint", "room_id", "room_name", "capacity")
                                              if key in search})
                    if all(search.get(key) for key in ("starts_at", "ends_at")):
                        self.draft.fields["search_window"] = {key: search[key] for key in ("starts_at", "ends_at")}
                        self.draft.fields.setdefault("date_hint", search["starts_at"][:10])
                    self.draft.choice_kind, self.draft.choices = self.query.choice_kind, self.query.choices
                if intent in ("reschedule", "cancel") and self.query and self.query.choice_kind == "booking":
                    self.draft.choice_kind, self.draft.choices = self.query.choice_kind, self.query.choices
            draft = self.draft
            self.query = None
        else:
            self.pending = None
            if self.query is None or self.query.intent != intent or (
                meaning.selection is None and not meaning.room_info
            ):
                previous = self.query
                self.query = Draft(intent)
                if intent == "detail" and meaning.selection and previous and previous.choice_kind == "booking":
                    self.query.choice_kind, self.query.choices = previous.choice_kind, previous.choices
            draft = self.query

        if intent in WRITES:
            self.pending = None
        if meaning.room_info and meaning.intent == "continue":
            if draft.choice_kind != "room" or not draft.choices:
                return "目前没有可引用的会议室候选。可以先查空闲会议室，再问某间房的地址或容量。"
            if meaning.selection and meaning.selection <= len(draft.choices):
                return self.room_line(draft.choices[meaning.selection - 1])
            matches = [room for room in draft.choices if (meaning.room_id and meaning.room_id == room["room_id"])
                       or (meaning.room_name and meaning.room_name in room["name"])]
            if not meaning.selection and not meaning.room_id and not meaning.room_name and len(draft.choices) == 1:
                matches = draft.choices
            if len(matches) == 1:
                return self.room_line(matches[0])
            return "你想了解哪间会议室？请提供刚才列表里的序号或名称。"
        updates = meaning.model_dump(mode="json", exclude_none=True,
                                     exclude={"intent", "selection", "room_info", "recent_booking",
                                              "target_date_hint", "use_booking_search"})
        if intent in ("reschedule", "cancel"):
            if meaning.target_date_hint:
                bounds = whole_day(meaning.target_date_hint, datetime.now(BEIJING).date())
                if bounds:
                    updates.setdefault("target_start", bounds["starts_at"])
                    updates.setdefault("target_end", bounds["ends_at"])
                elif not meaning.booking_id and not (meaning.target_start and meaning.target_end):
                    return self.needs("booking_target_date_unresolved", ("booking_target",),
                                      "核对要修改的会议原定日期。",
                                      "请告诉我这场会议的原定日期或申请编号。")
            if meaning.use_booking_search and self.booking_search and not (
                meaning.booking_id or meaning.target_date_hint or meaning.target_start or meaning.target_end
            ):
                for source, target in (("starts_at", "target_start"), ("ends_at", "target_end"),
                                       ("room_id", "target_room_id"), ("subject", "target_subject")):
                    if value := self.booking_search.scope.get(source):
                        updates.setdefault(target, value)
                if intent == "reschedule" and meaning.keep_original_time and self.booking_search.complete:
                    candidates = self.booking_search.items
                    if len(candidates) == 1:
                        updates["booking_id"] = candidates[0]["booking_id"]
        if intent in ("reschedule", "cancel", "detail") and meaning.recent_booking and not meaning.booking_id:
            if target := self.last_written_booking or self.last_booking:
                updates["booking_id"] = target
        for key in ("starts_at", "ends_at", "target_start", "target_end"):
            if key in updates:
                value = datetime.fromisoformat(updates[key])
                if value.microsecond:
                    return "请提供精确到整秒的时间。"
                updates[key] = value.astimezone(BEIJING).isoformat(timespec="seconds")
        fields = draft.fields
        if ("date_hint" in updates and updates["date_hint"] != fields.get("date_hint")
                and not fields.get("starts_at", "").startswith(updates["date_hint"] + "T")):
            for key in ("starts_at", "ends_at"):
                if key not in updates:
                    fields.pop(key, None)
        if intent == "rooms" and "day_part" in updates and updates["day_part"] != fields.get("day_part"):
            for key in ("starts_at", "ends_at"):
                if key not in updates:
                    fields.pop(key, None)
        if intent != "rooms" and ("starts_at" in updates or "ends_at" in updates):
            if "date_hint" not in updates:
                fields.pop("date_hint", None)
            if "keep_original_time" not in updates:
                fields.pop("keep_original_time", None)
        if "booking_id" in updates:
            for key in ("target_subject", "target_start", "target_end", "target_room_id"):
                fields.pop(key, None)
        if {"target_subject", "target_start", "target_end", "target_room_id"} & updates.keys() and "booking_id" not in updates:
            fields.pop("booking_id", None)
        if {"target_start", "target_end"} & updates.keys() and "target_room_id" not in updates:
            fields.pop("target_room_id", None)
        if "room_name" in updates and "room_id" not in updates:
            fields.pop("room_id", None)
        if "room_id" in updates and "room_name" not in updates:
            fields.pop("room_name", None)
        if "content" in updates and "clear_content" not in updates:
            fields.pop("clear_content", None)
        if "members" in updates and "member_mode" not in updates:
            fields.pop("member_mode", None)
        if meaning.selection is not None:
            if meaning.selection > len(draft.choices):
                return self.needs("invalid_selection", ("selection",), "请用户用有效的候选序号或名称。",
                                  "当前候选里没有这个序号，请用列表中的序号或直接说出名称。")
            choice = draft.choices[meaning.selection - 1]
            match draft.choice_kind:
                case "room":
                    updates.setdefault("room_id", choice["room_id"])
                    fields.pop("room_name", None)
                    if "room_id" not in meaning.model_fields_set:
                        updates.pop("room_name", None)
                case "booking":
                    updates.setdefault("booking_id", choice["booking_id"])
                case "host":
                    draft.resolved_people[choice["person_id"]] = choice
                    updates.setdefault("host", choice["person_id"])
                case "member":
                    draft.resolved_people[choice["person_id"]] = choice
                    updates.setdefault("members", [
                        choice["person_id"] if member == choice["query"] else member
                        for member in fields["members"]
                    ])
        fields.update(updates)
        if attendee_source and intent == "create":
            draft.attendee_source = attendee_source
        draft.choices = []
        draft.choice_kind = ""
        try:
            if intent == "rooms":
                return await self.rooms(draft)
            if intent == "bookings":
                return await self.bookings(draft)
            if intent == "detail":
                return await self.detail(draft)
            if self.uncertain:
                return "上一笔写入的结果仍待核实。请先回复「核实结果」，确认后再提交其他申请。"
            if intent == "create":
                return await self.create(draft)
            return await self.change(draft)
        except ToolFailure as error:
            return f"暂时无法完成这一步。{error}"

    @staticmethod
    def validate_times(fields: dict, *, future: bool = False) -> str | None:
        times = [datetime.fromisoformat(fields[key]) if fields.get(key) else None for key in ("starts_at", "ends_at")]
        start, end = times
        if any(value and value.microsecond for value in times):
            return "请提供精确到整秒的时间。"
        if start and end and end <= start:
            return "结束时间需要晚于开始时间。请补充正确的起止时间。"
        if future and start and start <= datetime.now(BEIJING):
            return "开始时间已经过去，请给一个未来的时间。"
        return None

    async def available_rooms(self, fields: dict, *, capacity: int, intervals: bool = False) -> list[dict]:
        room_id = fields.get("room_id")
        limit = 100 if room_id else PAGE_SIZE + 1
        offset = 0
        while True:
            options = {"mode": "intervals", "min_duration_minutes": fields.get("duration_minutes")} if intervals else {}
            items = (await self.call("find_available_rooms", starts_at=fields["starts_at"], ends_at=fields["ends_at"],
                                     min_capacity=capacity, name=fields.get("room_name"), limit=limit, offset=offset,
                                     **options))["items"]
            if not room_id:
                return items
            matches = [room for room in items if room["room_id"] == room_id]
            if matches or len(items) < limit:
                return matches
            offset += limit

    @staticmethod
    def room_line(item: dict) -> str:
        label = f"{item['name']}（{item['capacity']} 人，{item['address']}，{item['room_id']}）"
        if "free_intervals" not in item:
            return label
        def spans(key: str) -> str:
            return "、".join(window(bounds).removesuffix("（北京时间）") for bounds in item[key])
        return f"{label}\n   空闲：{spans('free_intervals')}\n   已占用：{spans('busy_intervals') or '无'}"

    async def rooms(self, draft: Draft) -> str:
        fields = draft.fields
        if fields.get("day_part") and fields.get("date_hint") and not (fields.get("starts_at") and fields.get("ends_at")):
            bounds = whole_day(fields["date_hint"], datetime.now(BEIJING).date())
            if bounds:
                part = fields["day_part"]
                defaults = DAY_PARTS[part]
                try:
                    start, end = os.getenv(f"MEETING_{part.upper()}_HOURS", "-".join(defaults)).split("-", maxsplit=1)
                    times = [time.fromisoformat(value) for value in (start, end)]
                    if any(value.tzinfo or value.microsecond for value in times) or times[0] >= times[1]:
                        raise ValueError("Invalid configured day part")
                except ValueError:
                    times = [time.fromisoformat(value) for value in defaults]
                day = datetime.fromisoformat(bounds["starts_at"]).date()
                for key, value in zip(("starts_at", "ends_at"), times):
                    fields.setdefault(key, datetime.combine(day, value, BEIJING).isoformat(timespec="seconds"))
                fields["flexible"] = True
        if not fields.get("starts_at") or not fields.get("ends_at"):
            date = fields.get("date_hint", "")
            needed = tuple(key for key in ("starts_at", "ends_at") if not fields.get(key))
            return self.needs("room_time_incomplete", needed, "保留已知日期，确认还缺的时刻。",
                              f"想查{date}几点到几点的空闲会议室？")
        if error := self.validate_times(fields):
            return error
        items = await self.available_rooms(fields, capacity=fields.get("capacity", 1),
                                           intervals=bool(fields.get("flexible") or fields.get("duration_minutes")))
        if not items:
            return f"{window(fields)}没有符合条件的空闲会议室。可以换个时段或调整人数。"
        return self.choose(draft, "room", items, self.room_line, f"{window(fields)}可用的会议室：")

    async def bookings(self, draft: Draft) -> str:
        fields = draft.fields
        if hint := fields.get("date_hint"):
            for key, value in whole_day(hint, datetime.now(BEIJING).date()).items():
                fields.setdefault(key, value)
        if error := self.validate_times(fields):
            return error
        if fields.get("date_hint") and not (fields.get("starts_at") and fields.get("ends_at")):
            return self.needs("booking_date_unresolved", ("date_range",), "如果原话已给日期，先按北京时间解析。",
                              "我还缺一个可查询的日期范围。你想查哪天的预约？")
        subject = fields.get("target_subject")
        filtered = any(fields.get(key) for key in ("starts_at", "ends_at", "status", "room_id", "target_subject"))
        status = fields.get("status") or (None if filtered else "等待")
        items = await self.search_bookings(fields, subject=subject, status=status)
        scope = {key: fields[key] for key in ("starts_at", "ends_at", "room_id") if fields.get(key)}
        scope.update(subject=subject, status=status)
        self.booking_search = BookingSearch(scope, items[:PAGE_SIZE], len(items) <= PAGE_SIZE)
        day = None
        if fields.get("starts_at") and fields.get("ends_at"):
            start, end = (datetime.fromisoformat(fields[key]).astimezone(BEIJING) for key in ("starts_at", "ends_at"))
            if start.time() == time.min and end == start + timedelta(days=1):
                day = start.date().isoformat()
        if not items:
            if day:
                return f"{day} 没有查到你创建、主持或参加的预约。"
            return "这个范围内没有查到你的预约。" if filtered else "你目前没有待开始的预约。"
        title = f"{day} 的会议室安排：" if day else ("查到这些预约：" if filtered else "你待开始的预约：")
        return self.choose(draft, "booking", items, booking_line, title)

    async def search_bookings(self, fields: dict, *, subject: str | None, status: str | None,
                              editable: bool = False) -> list[dict]:
        limit = 100 if subject or editable else PAGE_SIZE + 1
        matches = []
        offset = 0
        while True:
            items = (await self.call("list_my_bookings", starts_at=fields.get("starts_at"), ends_at=fields.get("ends_at"),
                                     room_id=fields.get("room_id"), status=status, limit=limit, offset=offset))["items"]
            matches.extend(item for item in items if (not subject or subject_matches(item["subject"], subject))
                           and (not editable or self.person_id in (item["host_id"], item["created_by"])))
            if len(matches) > PAGE_SIZE or len(items) < limit:
                return matches[:PAGE_SIZE + 1]
            offset += limit

    async def detail(self, draft: Draft) -> str:
        if not draft.fields.get("booking_id") and draft.fields.get("target_subject"):
            fields = draft.fields
            if fields.get("target_start"):
                fields["starts_at"] = fields["target_start"]
            if fields.get("target_end"):
                fields["ends_at"] = fields["target_end"]
            return await self.bookings(draft)
        if not draft.fields.get("booking_id"):
            return self.needs("booking_target_missing", ("booking_id",), "可以用用户给的编号或按主题、日期查找。",
                              "请告诉我申请编号，或先查你的预约。")
        item = await self.call("get_booking", booking_id=draft.fields["booking_id"])
        self.last_booking = item["booking_id"]
        members = "、".join(person["name"] for person in item["attendees"] if person["role"] == "成员") or "无"
        return (f"{booking_line(item)}\n主持人：{item['host_name']}｜预订人：{item['creator_name']}｜状态：{item['status']}"
                f"\n其他与会人员：{members}\n会议内容：{item.get('content') or '未填写'}")

    async def resolve_person(self, draft: Draft, name: str, kind: str) -> dict | str:
        if name in (self.person_id, "我", "自己", "本人"):
            return {"person_id": self.person_id, "name": "你"}
        items = (await self.call("find_people", name=name, limit=PAGE_SIZE + 1))["items"]
        if not items:
            return self.needs("person_not_found", (kind,), "核对原话中的姓名；名字无误时请用户提供更准确的姓名。",
                              f"没有找到「{name}」。请提供准确的姓名。")
        if len(items) == 1:
            return items[0]
        for item in items:
            item["query"] = name

        def person_line(item: dict) -> str:
            return " · ".join(filter(None, (item["name"], item.get("job_title"), item.get("job_level"), item["person_id"])))
        return self.choose(draft, kind, items, person_line, f"「{name}」匹配到多位人员，请选一位。")

    async def create(self, draft: Draft) -> str:
        fields = draft.fields.copy()
        host_name = fields.get("host") or self.person_id
        member_names = fields.get("members", [])
        if host_name in (self.person_id, "我", "自己", "本人"):
            member_names = [name for name in member_names if name not in (self.person_id, "我", "自己", "本人")]
        if fields.get("attendee_count", 1) > 1 + len(set(member_names)):
            return self.needs("attendees_incomplete", ("members",),
                              "从草稿 attendee_source 或用户原话补提取姓名。如果仍缺姓名，再向用户询问。",
                              "参会名单还没有凑齐，请告诉我其他与会人员的姓名。")
        if (fields.get("flexible") and fields.get("duration_minutes") and fields.get("search_window")
                and not fields.get("starts_at") and not fields.get("ends_at")):
            bounds = fields["search_window"]
            rooms = await self.available_rooms({**fields, **bounds}, capacity=max(
                fields.get("capacity", 1), fields.get("attendee_count", 1), 1 + len(member_names),
            ), intervals=True)
            if len(rooms) > 1 and not fields.get("any_room"):
                return self.choose(draft, "room", rooms, self.room_line, "这些会议室有合适的空闲段，你想选哪间？")
            for room in rooms:
                for slot in room["free_intervals"]:
                    left, right = (datetime.fromisoformat(slot[key]) for key in ("starts_at", "ends_at"))
                    left = max(left, datetime.now(BEIJING).replace(second=0, microsecond=0) + timedelta(minutes=1))
                    end = left + timedelta(minutes=fields["duration_minutes"])
                    if end <= right:
                        fields.update(room_id=room["room_id"], starts_at=left.isoformat(timespec="seconds"),
                                      ends_at=end.isoformat(timespec="seconds"))
                        break
                if fields.get("starts_at"):
                    break
            if not fields.get("starts_at"):
                return "搜索范围内没有符合时长和人数的空闲段，请调整范围或会议时长。"
        if fields.get("starts_at") and not fields.get("ends_at") and fields.get("duration_minutes"):
            end = datetime.fromisoformat(fields["starts_at"]) + timedelta(minutes=fields["duration_minutes"])
            fields["ends_at"] = end.isoformat(timespec="seconds")
        missing = []
        if not fields.get("starts_at") and not fields.get("ends_at"):
            day = fields.get("date_hint")
            time_needed = "开始时间" if fields.get("duration_minutes") else "起止时间"
            missing.append(f"{day} 的{time_needed}" if day else time_needed)
        elif not fields.get("starts_at"):
            missing.append("开始时间")
        elif not fields.get("ends_at"):
            missing.append("结束时间")
        if not fields.get("subject"):
            missing.append("会议主题")
        if missing:
            needed = tuple(key for key in ("starts_at", "ends_at", "subject")
                           if not fields.get(key) and not (key == "ends_at" and fields.get("duration_minutes")))
            return self.needs("booking_fields_missing", needed, "保留草稿，只补齐缺失的条件。",
                              f"还需要{'和'.join(missing)}。可以直接回复补充。")
        if error := self.validate_times(fields, future=True):
            return error

        people = []
        for kind, name in [("host", host_name), *(("member", name) for name in member_names)]:
            selected = draft.resolved_people.get(name)
            person = selected or await self.resolve_person(draft, name, kind)
            if isinstance(person, str):
                return person
            people.append(person)
            draft.resolved_people[name] = person
        host, *members = people
        member_ids = [person["person_id"] for person in members]
        if host["person_id"] in member_ids or len(set(member_ids)) != len(member_ids):
            return self.needs("duplicate_attendee", ("members",), "请用户核对其他与会人员名单。",
                              "与会人员有重复，或名单里包含主持人。请核对其他与会人员名单。")

        if fields.get("attendee_count", len(people)) != len(people):
            return self.needs("attendees_mismatch", ("attendee_count", "members"),
                              "请核对参会总人数和已经识别到的名单。",
                              "参会名单和你给的总人数不一致，请核对一下。")
        capacity = max(fields.get("capacity", 1), len(people))
        rooms = await self.available_rooms(fields, capacity=capacity)
        if not rooms:
            return "当前条件下查不到可用的会议室。可以换个时段、会议室或人数。"
        if len(rooms) > 1 and not fields.get("any_room"):
            return self.choose(draft, "room", rooms, self.room_line, "这些会议室可用，你想订哪间？")
        room = rooms[0]
        arguments = {key: fields[key] for key in ("subject", "starts_at", "ends_at")}
        arguments.update(room_id=room["room_id"], host_id=host["person_id"], member_ids=member_ids)
        if fields.get("content"):
            arguments["content"] = fields["content"]
        names = "、".join(person["name"] for person in members) or "无"
        summary = (f"主题「{fields['subject']}」\n{window(fields)}\n会议室：{self.room_line(room)}"
                   f"\n主持人：{host['name']}\n其他与会人员：{names}｜所需座位：{capacity} 人")
        if fields.get("content"):
            summary += f"\n会议内容：{fields['content']}"
        return self.stage(Application("create", arguments, summary))

    async def change(self, draft: Draft) -> str:
        fields = draft.fields
        booking_id = fields.get("booking_id")
        if not booking_id:
            if not any(fields.get(key) for key in ("target_subject", "target_start", "target_end", "target_room_id")):
                return self.needs("booking_target_missing", ("booking_target",), "可按编号、主题或会议原定日期定位。",
                                  "你想操作哪场会议？可以提供申请编号、会议主题或原定日期。")
            target = {"starts_at": fields.get("target_start"), "ends_at": fields.get("target_end"),
                      "room_id": fields.get("target_room_id")}
            if error := self.validate_times(target):
                return error
            matches = await self.search_bookings(target, subject=fields.get("target_subject"), status="等待", editable=True)
            if not matches:
                return self.needs("booking_target_not_found", ("booking_target",),
                                  "用已展示的预约核对简称，或请用户补充编号、主题或原定日期。",
                                  "没有找到符合条件且你有权操作的待开始预约。请补充申请编号或原定日期。")
            if len(matches) > 1:
                choices = self.choose(draft, "booking", matches, booking_line, "请选一下你要操作的预约。")
                return self.needs("booking_target_multiple", ("booking_target",),
                                  "请选择候选序号、编号或主题。", choices)
            booking_id = fields["booking_id"] = matches[0]["booking_id"]
        item = await self.call("get_booking", booking_id=booking_id)
        if self.person_id not in (item["host_id"], item["created_by"]):
            return "只有这场会议的主持人或预订人可以改期或取消。"
        if item["status"] != "等待":
            return f"这场会议的状态是「{item['status']}」，只有待开始的预约可以改期或取消。"
        if draft.intent == "cancel":
            return self.stage(Application("cancel", {"booking_id": booking_id}, booking_line(item), item))

        if fields.get("keep_original_time"):
            if not fields.get("date_hint"):
                return self.needs("reschedule_date_missing", ("date_hint",), "询问要改到哪天。",
                                  "想保留原时段、改到哪一天？")
            bounds = whole_day(fields["date_hint"], datetime.now(BEIJING).date())
            if not bounds:
                return self.needs("reschedule_date_invalid", ("date_hint",), "核对新的日期。", "请提供有效的新日期。")
            new_day = datetime.fromisoformat(bounds["starts_at"]).date()
            original_start = datetime.fromisoformat(item["starts_at"]).astimezone(BEIJING)
            shift = new_day - original_start.date()
            fields = {**fields, **{
                key: (datetime.fromisoformat(item[key]).astimezone(BEIJING) + shift).isoformat(timespec="seconds")
                for key in ("starts_at", "ends_at")
            }}
        if fields.get("date_hint") and not (fields.get("starts_at") and fields.get("ends_at")):
            needed = tuple(key for key in ("starts_at", "ends_at") if not fields.get(key))
            return self.needs("reschedule_time_incomplete", needed, "保留已知的新日期，补齐改后的时段。",
                              f"新日期是 {fields['date_hint']}。请告诉我新时段的开始和结束时间。")
        changes = {key: fields[key] for key in ("starts_at", "ends_at", "room_id") if fields.get(key)}
        if "subject" in fields:
            changes["subject"] = fields["subject"]
        if fields.get("clear_content"):
            changes["clear_content"] = True
        elif "content" in fields:
            changes["content"] = fields["content"]
        original_members = {person["person_id"]: person["name"] for person in item["attendees"] if person["role"] == "成员"}
        members = original_members.copy()
        if "members" in fields:
            mode = fields.get("member_mode", "replace")
            if mode == "replace":
                members.clear()
            for name in fields["members"]:
                person = draft.resolved_people.get(name) or await self.resolve_person(draft, name, "member")
                if isinstance(person, str):
                    return person
                draft.resolved_people[name] = person
                if mode == "remove":
                    members.pop(person["person_id"], None)
                else:
                    members[person["person_id"]] = person["name"]
            if item["host_id"] in members:
                return "其他与会人员名单里包含主持人，请调整名单。"
            changes["member_ids"] = list(members)
        if not changes and not fields.get("room_name") and not fields.get("any_room"):
            return self.needs("booking_changes_missing", ("changes",), "询问用户想修改的字段。",
                              f"找到{booking_line(item)}。你想修改哪项信息？")
        new = {**item, **changes}
        if error := self.validate_times(new, future=True):
            return error
        if fields.get("room_name") or fields.get("any_room") or (
            changes.get("room_id") and changes["room_id"] != item["room_id"]
        ):
            rooms = await self.available_rooms({**new, "room_id": changes.get("room_id"), "room_name": fields.get("room_name")},
                                               capacity=1 + len(members))
            if not rooms:
                return f"新条件下查不到可用会议室。原预约仍在：{booking_line(item)}"
            if len(rooms) > 1 and not fields.get("any_room"):
                return self.choose(draft, "room", rooms, self.room_line, "请选择改期后的会议室。")
            room = rooms[0]
            changes["room_id"] = new["room_id"] = room["room_id"]
            new["room_name"], new["room_address"] = room["name"], room["address"]
        if self.matches_changes(item, changes):
            return "新安排和原预约相同。请告诉我想修改的信息。"
        summary = f"原安排：{booking_line(item)}\n新安排：{booking_line(new)}"
        if "content" in changes or "clear_content" in changes:
            summary += f"\n会议内容：{item.get('content') or '未填写'} → {changes.get('content') or '清空'}"
        if "member_ids" in changes:
            summary += f"\n其他与会人员：{'、'.join(original_members.values()) or '无'} → {'、'.join(members.values()) or '无'}"
        return self.stage(Application("reschedule", {"booking_id": booking_id, **changes}, summary, item))

    @staticmethod
    def matches_changes(item: dict, changes: dict) -> bool:
        for key, value in changes.items():
            if key == "member_ids":
                actual = {person["person_id"] for person in item["attendees"] if person["role"] == "成员"}
                if actual != set(value):
                    return False
            elif key == "clear_content":
                if item.get("content") is not None:
                    return False
            elif key in ("starts_at", "ends_at"):
                if datetime.fromisoformat(item[key]) != datetime.fromisoformat(value):
                    return False
            elif item.get(key) != value:
                return False
        return True

    async def matching_window_ids(self, arguments: dict) -> frozenset[str]:
        ids = set()
        offset = 0
        while True:
            items = (await self.call("list_my_bookings", starts_at=arguments["starts_at"], ends_at=arguments["ends_at"],
                                     limit=100, offset=offset))["items"]
            ids.update(item["booking_id"] for item in items)
            if len(items) < 100:
                return frozenset(ids)
            offset += 100

    def stage(self, application: Application) -> str:
        self.pending = application
        return f"请核对{self.application_label(application)}申请：\n{application.summary}\n回复「确认」提交，或直接告诉我要修改的内容。"

    @staticmethod
    def application_label(application: Application) -> str:
        if application.intent == "reschedule" and application.arguments.keys() & {"subject", "content", "clear_content", "member_ids"}:
            return "修改"
        return LABELS[application.intent]

    async def confirm(self) -> str:
        if self.uncertain:
            return await self.reconcile()
        application = self.pending
        if not application:
            return "当前没有待确认的申请。请先告诉我你想办理什么。"
        self.pending = None
        self.query = None
        try:
            if application.original:
                item = await self.call("get_booking", booking_id=application.arguments["booking_id"])
                if item != application.original:
                    return "预约在确认前发生了变化。请回复「继续申请」重新核对。"
            else:
                application.known_ids = await self.matching_window_ids(application.arguments)
        except ToolFailure as error:
            self.pending = application
            return f"提交前核对失败，这次没有写入。{error} 可以稍后再回复「确认」。"
        self.uncertain = application
        if self.before_write:
            try:
                await self.before_write()
            except OSError:
                self.uncertain = None
                self.pending = application
                return "暂时无法保存提交状态，这次没有提交。"
        try:
            item = await self.call(WRITES[application.intent], **application.arguments)
        except ToolFailure as error:
            code = error.result.get("code")
            if code in UNCERTAIN:
                return f"提交结果暂时无法确认。{error} 请回复「核实结果」查询，避免重复提交。"
            self.uncertain = None
            if code in {"DATABASE_BUSY", "DATABASE_READONLY"}:
                self.pending = application
                return f"这次没有保存。{error} 恢复后可以重新回复「确认」。"
            if code == "ROOM_TIME_CONFLICT":
                original = "原预约仍保留。" if application.intent == "reschedule" else ""
                return f"{LABELS[application.intent]}未成功。该会议室在这个时段已被占用。{original}可以告诉我新的房间或时间。"
            return f"{self.application_label(application)}未成功。{error} 可以补充或修改条件后重新确认。"
        return self.complete(application, item)

    def complete(self, application: Application, item: dict) -> str:
        self.last_booking = item["booking_id"]
        self.last_written_booking = item["booking_id"]
        self.draft = None
        self.uncertain = None
        return f"已{self.application_label(application)}{booking_line(item)}。"

    async def reconcile(self) -> str:
        application = self.uncertain
        if application is None:
            return "当前没有待核实的申请。"
        arguments = application.arguments
        try:
            if application.intent == "create":
                items = (await self.call("list_my_bookings", starts_at=arguments["starts_at"],
                                         ends_at=arguments["ends_at"], limit=100))["items"]
                matches = [item for item in items if item["booking_id"] not in application.known_ids
                           and item["created_by"] == self.person_id and item["status"] != "取消"
                           and all(item[key] == arguments[key] for key in ("room_id", "host_id", "subject", "starts_at", "ends_at"))]
                if len(matches) == 1:
                    detail = await self.call("get_booking", booking_id=matches[0]["booking_id"])
                    members = {person["person_id"] for person in detail["attendees"] if person["role"] == "成员"}
                    if members == set(arguments["member_ids"]) and detail.get("content") == arguments.get("content"):
                        return self.complete(application, detail)
            else:
                item = await self.call("get_booking", booking_id=arguments["booking_id"])
                if application.intent == "cancel":
                    if item["status"] == "取消":
                        return self.complete(application, item)
                elif item["status"] != "取消" and self.matches_changes(item, arguments):
                    return self.complete(application, item)
        except ToolFailure as error:
            return f"暂时无法核实结果。{error} 请稍后再试。"
        return "查询结果暂时无法确认上一笔操作。请稍后再核实，或在核对预约列表后回复「结束核实」清除这笔待核实记录。"
