import asyncio
import json
import re
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from jsonschema import Draft202012Validator
from mcp import Client
from pydantic import BaseModel, ValidationError, create_model

from api.models import BEIJING
from mcp_server.client import ACTOR_META_KEY

from .mcp_connection import MCP_CONNECTION_ERRORS
from .memory import ConversationTurn, StoredTurn, turn_data
from .model import AgentError, ChatModel
from .prompt_loader import GROUP_CONTEXT, load_prompt
from .workflow import RELATIVE_DAYS, WRITES, BookingWorkflow, day_part_window, whole_day
from .workflow_models import DraftUserMessage, Interpretation, MeetingQuestion, WorkflowIssue


@dataclass(frozen=True)
class MeetingResult:
    text: str
    issue: WorkflowIssue | None
    protected: bool
    read_status: bool = False


type QueryIntent = Literal["bookings", "rooms"]


def field_schema(name: str, fields: set[str]) -> type[BaseModel]:
    definitions = {
        key: (info.rebuild_annotation(), None)
        for key, info in Interpretation.model_fields.items() if key in fields
    }
    return create_model(name, **definitions)


def follow_up_schema(fields: tuple[str, ...]) -> type[BaseModel] | None:
    if set(fields) == {"booking_target"}:
        return BOOKING_TARGET_FIELDS
    if not fields or not set(fields) <= {"subject", "starts_at", "ends_at"}:
        return None
    allowed = set(fields)
    if allowed & {"starts_at", "ends_at"}:
        allowed.update(("starts_at", "ends_at", "date_hint", "date_reference", "duration_minutes", "flexible", "day_part"))
    return field_schema("MeetingFollowUp", allowed)


CREATE_FIELDS = field_schema("NewBookingFields", {
    "starts_at", "ends_at", "start_clock", "end_clock", "date_hint", "date_reference", "duration_minutes", "subject", "host", "members",
    "content", "capacity", "room_id", "room_name", "any_room", "selection", "attendee_count", "flexible", "day_part", "member_mode",
})
BOOKING_TARGET_FIELDS = field_schema("BookingTargetFields", {
    "booking_id", "selection", "target_subject", "target_date_hint", "target_start", "target_end",
})
BOOKING_SUBJECT_FIELDS = field_schema("BookingSubjectReply", {"subject"})
QUERY_FIELDS = {
    "bookings": field_schema("MyBookingsQuery", {"date_hint", "starts_at", "ends_at", "target_subject", "status", "room_id"}),
    "rooms": field_schema("AvailableRoomsQuery", {"date_hint", "starts_at", "ends_at", "day_part", "capacity",
                                                  "room_name", "room_id", "duration_minutes", "flexible"}),
}
DAY_PART_WORDS = {"上午": "morning", "下午": "afternoon", "晚上": "evening"}
FLOOR_ROOM_NAME = re.compile(r"([0-9一二三四五六七八九十两]+[楼层])\s*(?:的\s*)?会议室")
REMOVE_ONE_MEMBER = re.compile(r"(?:那就|那|就|请)?\s*(?:不要|去掉|删掉|移除)\s*(.+?)(?:了|吧)?[。！!]?")
NUMERIC_HOUR_RANGE = re.compile(
    r"(?<![\d.])(?P<part>早上|上午|下午|晚上)?\s*(?P<start>\d{1,2})\s*点"
    r"\s*(?:到+|至|[-–—])\s*(?P<end_part>早上|上午|下午|晚上)?\s*(?P<end>\d{1,2})\s*点"
    r"(?!\s*(?:半|一刻|三刻|\d))"
)
ATTENDEE_COUNT = re.compile(r"(?:共\s*|一共\s*|总共\s*)?([0-9]+|[一二两三四五六七八九十])\s*(?:个\s*)?人")
EDIT_REFERENCE = re.compile(r"刚(?:刚|才)(?:预[订定]|订|改期|那|的)")
SAME_TIME_MOVE = re.compile(
    r"(?:改成|改到|改为|挪到|移到|推迟到|提前到)\s*"
    r"(?P<day>今天|明天|后天|\d{4}-\d{2}-\d{2}|(?:\d{1,2}\s*月\s*)?\d{1,2}\s*[日号])"
    r"[，,\s]*(?:的\s*)?(?:同样|相同|原来|原定|原)(?:的\s*)?(?:时间点?|时段)(?:的)?[。！!]?\s*"
)
QUOTED_CONTENT_EDIT = re.compile(r'(?:补充|更新|修改|编辑|添加|填写).*?会议内容\s*(?:为|是)?[\s，,:：]*["“「]([^"”」]+)["”」]\s*[。！!]?\s*$')
QUOTED_CONTENT_VALUE = re.compile(r'会议内容\s*(?:为|是)?[\s，,:：]*["“「]([^"”」]+)["”」]\s*[。！!]?\s*$')
CONFIRM_EXPRESSION = re.compile(
    r"(?:好(?:的)?[，,、\s]*)?(?:确认(?:提交|预[订定]|改期|修改|取消)?|"
    r"就(?:这么|这样)(?:预[订定]|订|改|取消|提交)(?:就?行|吧|可以)?|"
    r"按(?:这个|你列的)(?:安排|方案|申请)?提交吧?)"
)


class MeetingAgent:
    def __init__(self, client: Client, model: ChatModel, person_id: str | None, *, group: bool = False) -> None:
        self.client = client
        self.model = model
        self.person_id = person_id
        self.group = group
        self.quoted_message: str | None = None
        self.prompt = load_prompt("meeting_intent")
        self.history: deque[StoredTurn] = deque(maxlen=3)
        self.request_time: datetime | None = None
        self.validators: dict[str, Draft202012Validator] = {}
        self.workflow = BookingWorkflow(self.execute, person_id)
        self.lock = asyncio.Lock()
        self.trace = False
        self.read_status = False

    async def discover_tools(self) -> None:
        available = (await self.client.list_tools()).tools
        self.validators = {
            tool.name: Draft202012Validator({**tool.input_schema, "additionalProperties": False})
            for tool in available
        }
        required = {"find_available_rooms"} if self.group else {
            *WRITES.values(), "get_booking", "find_people", "find_available_rooms", "list_my_bookings",
        }
        if missing := required - self.validators.keys():
            raise AgentError(f"MCP 缺少会议室工具：{', '.join(sorted(missing))}")

    async def execute(self, name: str, arguments: dict) -> dict:
        if self.group and name != "find_available_rooms":
            return {"is_error": True, "code": "GROUP_TOOL_UNAVAILABLE", "message": "这项操作请在单聊里办理。"}
        validator = self.validators[name]
        if issue := next(validator.iter_errors(arguments), None):
            return {"is_error": True, "code": "INVALID_ARGUMENTS", "message": issue.message}
        try:
            meta = {ACTOR_META_KEY: self.person_id} if self.person_id else None
            result = await self.client.call_tool(name, arguments, meta=meta)
        except MCP_CONNECTION_ERRORS:
            return {"is_error": True, "code": "MCP_CALL_FAILED", "message": "会议工具连接中断或超时。"}
        data = result.structured_content
        if not isinstance(data, dict):
            return {"is_error": True, "code": "API_INVALID_RESPONSE", "message": "会议工具未返回结构化结果。"}
        if self.trace:
            http_status = f" (HTTP {data['http_status']})" if "http_status" in data else ""
            print(f"[工具] {name} → {data.get('code', '失败' if result.is_error else '成功')}{http_status}", flush=True)
        return {**data, "is_error": result.is_error}

    async def reply(self, text: str, *, trace: bool = False) -> str:
        return (await self.handle(text, trace=trace)).text

    async def handle(
        self, text: str, *, trace: bool = False, guidance: str | None = None,
        follow_up: "MeetingQuestion | None" = None, request_time: datetime | None = None, remember: bool = True,
        start_booking: bool = False, new_request: bool = False, query_intent: QueryIntent | None = None,
    ) -> MeetingResult:
        async with self.lock:
            self.trace = trace
            self.read_status = False
            self.request_time = request_time or datetime.now(BEIJING)
            self.workflow.issue = None
            critical = bool(self.workflow.pending or self.workflow.uncertain)
            if query_intent:
                answer = await self.interpret_request(text, guidance=guidance, new_request=True, query_intent=query_intent)
            else:
                answer = await self.respond(text, guidance=guidance, follow_up=follow_up,
                                            start_booking=start_booking, new_request=new_request)
            if remember:
                self.record_turn(text, answer)
            protected = critical or bool(self.workflow.pending or self.workflow.uncertain)
            protected = protected or bool(re.search(
                r"[ARP][0-9]{6}|^\d+\. |未成功|无法|没有|查不到|失败|冲突|无权", answer, re.MULTILINE,
            ))
            return MeetingResult(answer, self.workflow.issue, protected, self.read_status)

    def record_turn(self, text: str, answer: str) -> None:
        at = self.request_time.isoformat(timespec="seconds") if self.request_time else None
        self.history.append(ConversationTurn(text[:600], answer[:1000], at))

    def draft_message(self, text: str) -> DraftUserMessage:
        at = (self.request_time or datetime.now(BEIJING)).astimezone(BEIJING).isoformat(timespec="seconds")
        return DraftUserMessage(text[:600], at)

    async def recover_subject(self, text: str, question: MeetingQuestion) -> str | None:
        messages = [
            {"role": "system", "content": load_prompt("booking_subject")},
            {"role": "user", "content": json.dumps({"question": question.text, "reply": text}, ensure_ascii=False)},
        ]
        try:
            answer = await self.model.complete(messages, schema=BOOKING_SUBJECT_FIELDS, trace=self.trace)
            subject = (BOOKING_SUBJECT_FIELDS.model_validate_json(answer.visible_content()).subject or "").strip()
        except (AgentError, ValidationError):
            return None
        return subject if subject and subject in text else None

    async def respond(
        self, text: str, *, guidance: str | None = None, follow_up: "MeetingQuestion | None" = None,
        start_booking: bool = False, new_request: bool = False,
    ) -> str:
        if guidance or self.group:
            return await self.interpret_request(text, guidance=guidance, follow_up=follow_up, new_request=new_request)
        command = text.strip().rstrip("。！!").strip()
        workflow = self.workflow
        if command in {"确认", "确认提交", "确认预订", "确认改期", "确认修改", "确认取消"}:
            if command not in {"确认", "确认提交"} and workflow.pending:
                expected = {"create": {"确认预订"}, "reschedule": {"确认改期", "确认修改"}, "cancel": {"确认取消"}}
                if command not in expected[workflow.pending.intent]:
                    return "你说的操作和待确认申请不同，请核对后再回复「确认」。"
            return await workflow.confirm()
        if command in {"核实结果", "再试一次", "重试"} and workflow.uncertain:
            return await workflow.reconcile()
        if command == "核实结果":
            return await workflow.reconcile()
        if command == "结束核实" and workflow.uncertain:
            workflow.uncertain = None
            workflow.draft = None
            return "已结束本次核实。提交状态仍以预约列表为准。"
        if command in {"放弃申请", "不提交", "算了", "不用了"} and workflow.draft:
            workflow.pending = workflow.draft = None
            return "已放弃当前申请。"
        if command in {"继续申请", "修改申请"} and workflow.draft:
            workflow.query = None
            return await workflow.apply(Interpretation(intent="continue"), resume_write=True)
        if command in {"查我的预约", "我的预约"}:
            workflow.query = None
            return await workflow.apply(Interpretation(intent="bookings"))
        if match := re.fullmatch(r"(?:查(?:一下|看)?\s*)?(A[0-9]{6})", command, re.IGNORECASE):
            return await workflow.apply(Interpretation(intent="detail", booking_id=match[1].upper()))
        if workflow.last_booking and re.fullmatch(r"取消(?:刚才|刚刚)(?:的)?那场(?:会|会议|预约)?", command):
            return await workflow.apply(Interpretation(intent="cancel", booking_id=workflow.last_booking),
                                        user_message=self.draft_message(text), new_request=new_request)
        if match := re.fullmatch(r"(?:就|选)?第([一二三四五1-5])(?:个|间|位|场)?", command):
            number = "一二三四五".find(match[1]) + 1 if not match[1].isdigit() else int(match[1])
            current = workflow.query or workflow.draft
            if current and current.intent == "bookings":
                return await workflow.apply(Interpretation(intent="detail", selection=number))
            if current and current.intent == "rooms":
                return await workflow.apply(Interpretation(intent="continue", selection=number, room_info=True))
            return await workflow.apply(Interpretation(intent="continue", selection=number),
                                        user_message=self.draft_message(text))
        if command in {"好的", "可以", "好", "同意", "提交", "就这样", "没问题"} and workflow.pending:
            return workflow.needs("explicit_confirmation_needed", ("confirmation",),
                                  "请用户明确表示是否按当前申请提交。",
                                  "请回复「确认」提交这份申请。")
        return await self.interpret_request(text, follow_up=follow_up, start_booking=start_booking, new_request=new_request)

    async def interpret_request(
        self, text: str, *, guidance: str | None = None, follow_up: "MeetingQuestion | None" = None,
        start_booking: bool = False, new_request: bool = False, query_intent: QueryIntent | None = None,
    ) -> str:
        workflow = self.workflow
        request_time = self.request_time or datetime.now(BEIJING)
        now = request_time.isoformat(timespec="seconds")
        state = json.dumps({
            "workflow": workflow.context(include_draft_messages=not (new_request or start_booking)),
            "recent": [turn_data(turn) for turn in self.history],
            "question_shown_to_user": asdict(follow_up) if follow_up else None,
            **({"quoted_message": self.quoted_message} if self.quoted_message else {}),
        }, ensure_ascii=False, separators=(",", ":"))
        messages = [
            {"role": "system", "content": f"{self.prompt}\n当前北京时间 {now}。当前操作者 {self.person_id}。"},
            {"role": "user", "content": f"会话数据：{state}\n本轮用户消息：{text}"
             + (f"\n主助手的解析建议（仅供理解原话）：{guidance}" if guidance else "")},
        ]
        if self.group:
            messages[0]["content"] += f"\n{GROUP_CONTEXT}"
            messages[0]["content"] += "\n" + load_prompt("quoted_context").rstrip("\n")
        quoted_edit = QUOTED_CONTENT_EDIT.fullmatch(text.strip())
        if not query_intent and quoted_edit and EDIT_REFERENCE.search(text) and workflow.last_written_booking and not follow_up:
            prefix = text[:quoted_edit.start(1)]
            if not any(word in prefix for word in ("再", "同时", "并且", "改到", "换到", "改期")):
                return await workflow.apply(Interpretation(
                    intent="reschedule", recent_booking=True, content=quoted_edit[1],
                ), user_message=self.draft_message(text), new_request=new_request)
        try:
            explicit_create = not self.group and start_booking
            schema = follow_up_schema(follow_up.fields) if follow_up else None
            if query_intent:
                schema = QUERY_FIELDS[query_intent]
                messages[0]["content"] = load_prompt("read_query").format(now=now, intent=query_intent)
            elif explicit_create:
                schema = CREATE_FIELDS
                messages[0]["content"] = load_prompt("booking_create").rstrip("\n").format(now=now)
            elif schema:
                if schema is BOOKING_TARGET_FIELDS:
                    prompt = "booking_target"
                else:
                    prompt = "booking_follow_up"
                    if workflow.draft and workflow.draft.intent == "create" and not workflow.query:
                        schema = CREATE_FIELDS
                messages[0]["content"] = load_prompt(prompt).rstrip("\n").format(now=now)
                if self.group:
                    messages[0]["content"] += f"\n{GROUP_CONTEXT}"
            elif guidance and workflow.draft and workflow.draft.intent == "create" and not workflow.query:
                schema = CREATE_FIELDS
                messages[0]["content"] = load_prompt("booking_follow_up").rstrip("\n").format(now=now)
            if schema:
                result = await self.model.complete(messages, schema=schema, trace=self.trace)
                fields = schema.model_validate_json(result.visible_content()).model_dump(mode="json", exclude_none=True)
                if schema is BOOKING_TARGET_FIELDS:
                    draft = workflow.draft
                    choices = draft.choices if draft and draft.choice_kind == "booking" else (
                        workflow.booking_search.items if workflow.booking_search else []
                    )
                    candidate_ids = {item["booking_id"] for item in choices}
                    if (booking_id := fields.get("booking_id")) and booking_id not in candidate_ids and booking_id not in text.upper():
                        fields.pop("booking_id")
                    if not any(fields.values()):
                        fields["target_subject"] = text.strip()
                if (
                    not query_intent and not explicit_create and follow_up
                    and "subject" in follow_up.fields and not fields.get("subject")
                    and workflow.draft and workflow.draft.intent == "create"
                    and not workflow.draft.fields.get("subject")
                ):
                    # Use only the user's current answer to recover a title missed by the broader extraction.
                    if subject := await self.recover_subject(text, follow_up):
                        fields["subject"] = subject
                meaning = Interpretation.model_validate({"intent": query_intent or ("create" if explicit_create else "continue"), **fields})
            else:
                meaning = await self.model.interpret(messages, trace=self.trace)
        except AgentError as error:
            return f"暂时无法理解这条请求。{error}可以稍后重试。"
        except ValidationError:
            return "暂时无法理解这条请求，模型返回的信息格式有误。请把操作、日期和时间分开说清楚。"
        if self.trace:
            print(f"[意图] {meaning.intent}", flush=True)
        if self.group and (
            meaning.intent not in {"rooms", "continue", "unknown", "help"}
            or (meaning.intent == "continue" and workflow.query and workflow.query.intent != "rooms")
        ):
            return "群聊可以查空闲会议室。个人预约查询和办理请在单聊里继续。"
        if self.group and meaning.intent == "help":
            return "可以在群里查询空闲会议室、地址和容量。个人预约和办理请单聊。"
        if meaning.intent == "booking_status":
            self.read_status = True
            return await workflow.booking_status()
        if meaning.intent == "confirm" and not guidance and workflow.pending:
            command = text.strip().rstrip("。！!").strip()
            matches = {"create": ("预订", "预定", "订"), "reschedule": ("改",), "cancel": ("取消",)}
            named = {kind for kind, words in matches.items() if any(word in command for word in words)}
            if CONFIRM_EXPRESSION.fullmatch(command) and (not named or named == {workflow.pending.intent}) and not (
                command in {"确认预定"} and workflow.pending.intent != "create"
            ):
                return await workflow.confirm()
            return workflow.needs("explicit_confirmation_needed", ("confirmation",),
                                  "整句可能有附加条件。问还有什么要补充，提醒核对后单独回复确认。",
                                  "这句话可能还有要补充的内容。请先说完整，或单独回复「确认」提交。")
        if follow_up and meaning.intent == "continue" and set(follow_up.fields) & {
            "subject", "starts_at", "ends_at", "new_time_or_room",
        }:
            meaning = meaning.model_copy(update={"room_info": False})
        current = workflow.query or workflow.draft
        if follow_up and "room_id" in follow_up.fields and workflow.draft and workflow.draft.choice_kind == "room":
            for index, room in enumerate(workflow.draft.choices, 1):
                if text.strip() in (room["name"], room["room_id"]):
                    meaning = Interpretation(intent="continue", selection=index)
                    break
            if meaning.intent == "continue" and not meaning.room_info:
                current = workflow.draft
        if (move := SAME_TIME_MOVE.fullmatch(text.strip())) and (
            meaning.intent in {"reschedule", "continue"} or (current and current.intent == "bookings")
        ):
            day = move["day"]
            if match := re.fullmatch(r"(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*[日号]", day):
                try:
                    day = date(request_time.year, int(match[1] or request_time.month), int(match[2])).isoformat()
                except ValueError:
                    return workflow.needs("reschedule_date_invalid", ("date_hint",), "核对新的日期。", "请提供有效的新日期。")
            bounds = whole_day(day, request_time.date())
            if not bounds:
                return workflow.needs("reschedule_date_invalid", ("date_hint",), "核对新的日期。", "请提供有效的新日期。")
            meaning = Interpretation(
                intent="reschedule", date_hint=bounds["starts_at"][:10], keep_original_time=True,
                use_booking_search=bool(workflow.query and workflow.query.intent == "bookings"),
            )
        if meaning.intent == "reschedule":
            # The model chooses the operation. Preserve an explicit same-time constraint even if it omits the flag.
            same_time = meaning.keep_original_time or any(
                phrase in text for phrase in ("同样时间", "相同时间", "原时段", "同一时间", "原时间")
            )
            refers_to_listing = bool(
                workflow.query and workflow.query.intent == "bookings"
                and any(phrase in text for phrase in ("这个会议", "这场会议", "这次会议", "刚查到"))
            )
            hint = meaning.date_hint
            if meaning.date_reference in RELATIVE_DAYS and meaning.date_reference in text:
                hint = meaning.date_reference
            else:
                destinations = [day for day in RELATIVE_DAYS if any(
                    f"{verb}{day}" in text for verb in ("改成", "改到", "挪到", "改为", "移到")
                )]
                if len(destinations) == 1:
                    hint = destinations[0]
            if same_time and hint and (bounds := whole_day(hint, request_time.date())):
                meaning = meaning.model_copy(update={
                    "date_hint": bounds["starts_at"][:10], "keep_original_time": True,
                })
            if refers_to_listing and same_time and not (meaning.booking_id or meaning.selection):
                meaning = meaning.model_copy(update={
                    "use_booking_search": True, "recent_booking": False,
                    "target_date_hint": None, "target_start": None, "target_end": None,
                })
        if meaning.intent == "rooms" or (meaning.intent == "continue" and current and current.intent == "rooms"):
            days = [day for day in RELATIVE_DAYS if day in text]
            parts = [part for word, part in DAY_PART_WORDS.items() if word in text]
            slot = parts[0] if len(parts) == 1 else meaning.day_part
            hint = meaning.date_hint
            # Keep the model's interpretation of the time, but anchor a single explicit day to Beijing time.
            relative_day = days[0] if len(days) == 1 and not re.search(
                rf"{days[0]}\s*(?:之前|之后|以前|以后|起|以来)|"
                r"[到至]\s*(?:下?周|星期|(?:本|下)?月底|\d{4}-\d{2}-\d{2}|\d+\s*[月日号])",
                text,
            ) else None
            if relative_day:
                day = request_time.date() + timedelta(days=RELATIVE_DAYS[relative_day])
                hint = day.isoformat()
                if anchor := meaning.starts_at or meaning.ends_at:
                    shift = day - anchor.astimezone(BEIJING).date()
                    meaning = meaning.model_copy(update={
                        key: value.astimezone(BEIJING) + shift
                        for key in ("starts_at", "ends_at") if (value := getattr(meaning, key)) is not None
                    })
            explicit_bounds = meaning.starts_at and meaning.ends_at
            flexible = meaning.flexible
            if any(value in text for value in ("之间", "间有空", "都行", "有空的时段", "范围内", "任选")):
                flexible = True
            if slot and (not explicit_bounds or not re.search(r"[点时:：]", text)):
                flexible = True
                if hint:
                    meaning = meaning.model_copy(update={"starts_at": None, "ends_at": None})
            meaning = meaning.model_copy(update={"day_part": slot, "date_hint": hint, "flexible": flexible})
        if workflow.last_written_booking and EDIT_REFERENCE.search(text) and not meaning.booking_id and (
            meaning.intent == "reschedule" or meaning.intent == "continue" and not workflow.draft
        ) and (meaning.intent == "reschedule" or meaning.content is not None
               or meaning.clear_content or meaning.subject or meaning.members is not None):
            meaning = meaning.model_copy(update={"intent": "reschedule", "recent_booking": True})
        if meaning.intent == "reschedule" and (quoted := QUOTED_CONTENT_VALUE.search(text)):
            meaning = meaning.model_copy(update={"content": quoted[1]})
            reference = text[:quoted.start(1)]
            if re.search(r"(?:给|为)(?:这个|这场|这次)(?:会议|预约)", reference):
                if workflow.booking_search and not meaning.booking_id:
                    meaning = meaning.model_copy(update={"use_booking_search": True})
                if not re.search(r"改期|换房|换时间|改时间|改到|挪到|开始|结束|时段", reference):
                    meaning = meaning.model_copy(update={"starts_at": None, "ends_at": None, "date_hint": None})
        if meaning.intent in ("reschedule", "cancel") and not (
            meaning.booking_id or meaning.selection or meaning.recent_booking
        ):
            reference = re.split(r'["“「]', text, maxsplit=1)[0]
            days = [day for day in RELATIVE_DAYS if day in reference]
            detail_change = any((
                meaning.content is not None, meaning.subject is not None, meaning.members is not None,
                meaning.clear_content,
            ))
            if detail_change and not re.search(r"改到|改至|挪到|推迟到|提前到|时间改", reference):
                hint = meaning.target_date_hint or (days[0] if len(days) == 1 else meaning.date_hint)
                if hint:
                    updates = {"target_date_hint": hint, "date_hint": None}
                    if not re.search(r"改时间|换时间|时间|开始|结束|时段|几点|延长|缩短", reference):
                        updates.update(starts_at=None, ends_at=None)
                    meaning = meaning.model_copy(update=updates)
        attendee_source = None
        if meaning.intent == "create" or (meaning.intent == "continue" and current and current.intent == "create"):
            continuing = meaning.intent == "continue" or not (new_request or start_booking)
            existing = workflow.draft if continuing and workflow.draft and workflow.draft.intent == "create" else None
            if existing and (removal := REMOVE_ONE_MEMBER.fullmatch(text.strip())):
                name = removal[1].strip().strip("「」“”")
                if name in existing.fields.get("members", []):
                    meaning = meaning.model_copy(update={"member_mode": "remove", "members": [name]})
            if meaning.room_name and (floor := FLOOR_ROOM_NAME.fullmatch(meaning.room_name)):
                meaning = meaning.model_copy(update={"room_name": floor[1]})
            if workflow.query and workflow.query.intent == "rooms" and meaning.intent == "create":
                if not re.search(r"[点时:：]", text):
                    meaning = meaning.model_copy(update={"starts_at": None, "ends_at": None})
            previous_day = current.fields.get("date_hint") if meaning.intent == "continue" else None
            if not previous_day and meaning.intent == "continue" and current.fields.get("starts_at"):
                previous_day = current.fields["starts_at"][:10]
            time_text = text
            for value in (meaning.subject, meaning.content):
                if value:
                    time_text = time_text.replace(value, "")
            if not (meaning.room_name or meaning.room_id or meaning.selection or meaning.any_room):
                floors = set(FLOOR_ROOM_NAME.findall(time_text))
                existing_fields = existing.fields if existing else {}
                if not floors and existing and not any(existing_fields.get(key) for key in ("room_name", "room_id", "any_room")):
                    floors = {floor for message in existing.user_messages for floor in FLOOR_ROOM_NAME.findall(message.text)}
                if len(floors) == 1:
                    meaning = meaning.model_copy(update={"room_name": floors.pop()})
            relative_days = [day for day in RELATIVE_DAYS if day in time_text]
            hint = meaning.date_hint
            if previous_day and not (
                (meaning.date_reference and meaning.date_reference in time_text)
                or (hint and hint in time_text)
            ):
                hint = previous_day
            if len(relative_days) == 1:
                hint = relative_days[0]
            if hint and (bounds := whole_day(hint, request_time.date())):
                day = date.fromisoformat(bounds["starts_at"][:10])
                updates = {"date_hint": day.isoformat()}
                if anchor := meaning.starts_at or meaning.ends_at:
                    shift = day - anchor.astimezone(BEIJING).date()
                    updates.update({
                        key: value.astimezone(BEIJING) + shift
                        for key in ("starts_at", "ends_at") if (value := getattr(meaning, key)) is not None
                    })
                for key, clock in (("starts_at", meaning.start_clock), ("ends_at", meaning.end_clock)):
                    if clock is not None and clock.tzinfo is None and clock.microsecond == 0:
                        updates[key] = datetime.combine(day, clock, BEIJING)
                if "starts_at" not in updates and "ends_at" not in updates:
                    # Keep a literal clock range when the model recognized the booking but left times empty.
                    matches = list(NUMERIC_HOUR_RANGE.finditer(time_text))
                    if len(matches) == 1:
                        match = matches[0]
                        start, end = int(match["start"]), int(match["end"])
                        for_part = {"下午", "晚上"}
                        if match["part"] in for_part and start < 12:
                            start += 12
                        if (match["end_part"] or match["part"]) in for_part and end < 12:
                            end += 12
                        if 0 <= start < end < 24:
                            updates.update(starts_at=datetime.combine(day, time(start), BEIJING),
                                           ends_at=datetime.combine(day, time(end), BEIJING))
                meaning = meaning.model_copy(update=updates)
            if meaning.attendee_count is None and "座位" not in text and ("和" in text or "参会" in text or "参加" in text):
                match = ATTENDEE_COUNT.search(text)
                if match:
                    value = match[1]
                    count = int(value) if value.isdecimal() else "一二三四五六七八九十".index(value.replace("两", "二")) + 1
                    if 1 <= count <= 10000:
                        meaning = meaning.model_copy(update={"attendee_count": count})
            if (meaning.attendee_count or meaning.members) and (
                meaning.member_mode not in {"add", "remove"} or not (existing and existing.attendee_source)
            ):
                attendee_source = text[:600]
        if meaning.intent == "create" and meaning.duration_minutes is None:
            # Preserve an explicitly stated numeric duration when extraction omits it.
            units = re.findall(r"(?<![\d.])(\d{1,4})\s*(?:个\s*)?(小时|分钟)", text)
            if len(units) == 1:
                count, unit = units[0]
                minutes = int(count) * (60 if unit == "小时" else 1)
                if 1 <= minutes <= 10080:
                    meaning = meaning.model_copy(update={"duration_minutes": minutes})
        active_query = workflow.query
        if meaning.intent == "bookings" or (
            meaning.intent == "continue" and active_query and active_query.intent == "bookings"
        ):
            days = [day for day in RELATIVE_DAYS if day in text]
            day_only = len(days) == 1 and not re.search(
                r"\d|上午|下午|晚上|早上|中午|凌晨|半天|[一二两三四五六七八九十几]点|从|到|至|"
                r"之前|之后|以前|以后|起|以来|开始|这周|下周|本周|本月|下月|几天|一周|期间",
                text,
            )
            today = request_time.date()
            parts = [part for word, part in DAY_PART_WORDS.items() if word in text]
            part_hint = days[0] if len(days) == 1 else meaning.date_hint
            if len(parts) == 1 and len(days) <= 1 and part_hint and not re.search(r"[点:：到至]|之前|之后|以来", text) and (
                part_day := whole_day(part_hint, today)
            ):
                day = datetime.fromisoformat(part_day["starts_at"]).date()
                meaning = Interpretation.model_validate({
                    **meaning.model_dump(mode="json"), "date_hint": day.isoformat(), **day_part_window(day, parts[0]),
                })
            elif day_only:
                bounds = whole_day(days[0], today)
                # An explicit day in the current message overrides both model-generated bounds.
                meaning = Interpretation.model_validate({
                    **meaning.model_dump(mode="json"), "date_hint": bounds["starts_at"][:10], **bounds,
                })
            elif meaning.date_hint and (meaning.starts_at is None or meaning.ends_at is None):
                bounds = whole_day(meaning.date_hint, today)
                if bounds:
                    updates = {key: value for key, value in bounds.items() if getattr(meaning, key) is None}
                    meaning = Interpretation.model_validate({
                        **meaning.model_dump(mode="json"), "date_hint": meaning.date_hint, **updates,
                    })
        return await workflow.apply(
            meaning, attendee_source=attendee_source,
            resume_write=meaning.intent == "continue" and current is not None and current is workflow.draft,
            user_message=self.draft_message(text), new_request=new_request or start_booking,
        )
