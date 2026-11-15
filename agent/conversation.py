import asyncio
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from api.models import BEIJING

from .meeting import MeetingAgent, MeetingResult, QueryIntent
from .memory import AgentMemory, ConversationTurn, StoredTurn, turn_data
from .model import AgentError, ChatModel
from .prompt_loader import GROUP_CONTEXT, load_prompt
from .workflow_models import MeetingQuestion, WorkflowIssue, WorkflowMemory


class QueryPart(BaseModel):
    intent: QueryIntent
    text: str = Field(min_length=1, max_length=600)


class Route(BaseModel):
    route: Literal["chat", "meeting"]
    new_request: bool = False
    booking_status: bool = False
    start_booking: bool = False
    queries: list[QueryPart] = Field(default_factory=list, max_length=2)


class Coordination(BaseModel):
    action: Literal["ask", "answer", "retry"]
    message: str
    requested_fields: list[str] | None = None


class MainAgent:
    def __init__(self, model: ChatModel, connect_meeting: Callable[[], Awaitable[MeetingAgent]]) -> None:
        self.model = model
        self.connect_meeting = connect_meeting
        self.meeting: MeetingAgent | None = None
        self.memory: AgentMemory | None = None
        self.save_memory: Callable[[AgentMemory], Awaitable[None]] | None = None
        self.group = False
        self.quoted_message: str | None = None
        self.chat_history: deque[StoredTurn] = deque(maxlen=4)
        self.chat_summary = ""
        self.unsummarized_chat: deque[StoredTurn] = deque(maxlen=4)
        self.request_time: datetime | None = None
        self.last_route = "chat"
        self.lock = asyncio.Lock()
        self.routing_prompt = load_prompt("routing")
        self.chat_prompt = load_prompt("chat")
        self.memory_prompt = load_prompt("chat_summary")
        self.meeting_prompt = load_prompt("meeting_reply")
        self.coordination_prompt = load_prompt("coordination")

    @property
    def meeting_question(self) -> MeetingQuestion | None:
        return self.meeting.workflow.question if self.meeting else self.memory.workflow.question if self.memory else None

    def restore(self, memory: AgentMemory) -> None:
        self.memory = memory
        self.chat_history.extend(memory.chat_history)
        self.chat_summary = memory.chat_summary
        self.unsummarized_chat.extend(memory.unsummarized_chat)
        self.last_route = memory.last_route

    def snapshot(self) -> AgentMemory:
        return AgentMemory(
            chat_history=list(self.chat_history), chat_summary=self.chat_summary,
            unsummarized_chat=list(self.unsummarized_chat), last_route=self.last_route,
            meeting_history=list(self.meeting.history) if self.meeting else self.memory.meeting_history if self.memory else [],
            workflow=self.meeting.workflow.snapshot() if self.meeting else self.memory.workflow if self.memory else WorkflowMemory(),
        )

    async def checkpoint(self) -> None:
        if self.save_memory:
            await self.save_memory(self.snapshot())

    def channel_prompt(self, prompt: str) -> str:
        return f"{prompt}\n\n{GROUP_CONTEXT}" if self.group else prompt

    async def route(self, text: str, *, trace: bool) -> Route:
        workflow = self.meeting.workflow if self.meeting else self.memory.workflow if self.memory else WorkflowMemory()
        context = {
            "previous_mode": self.last_route,
            "meeting_waiting": bool(workflow.draft or workflow.query or workflow.uncertain),
            "confirmation_pending": bool(workflow.pending),
            "recent_booking_exists": bool(workflow.last_written_booking),
            "has_booking_search": bool(workflow.booking_search),
            "request_at": self.request_time.isoformat(timespec="seconds") if self.request_time else None,
            "booking_step": workflow.step(),
            "previous_meeting_question": asdict(self.meeting_question)
            if self.meeting_question else None,
            "previous_chat": turn_data(self.chat_history[-1]) if self.last_route == "chat" and self.chat_history else None,
            "message": text,
        }
        if self.quoted_message:
            context["quoted_message"] = self.quoted_message
        history = self.meeting.history if self.meeting else self.memory.meeting_history if self.memory else []
        if self.meeting_question and history:
            context["previous_meeting_message"] = turn_data(history[-1])["user"]
        messages = [
            {"role": "system", "content": self.channel_prompt(self.routing_prompt)},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        result = await self.model.complete(messages, schema=Route, trace=trace)
        return Route.model_validate_json(result.visible_content())

    async def chat(self, text: str, *, trace: bool) -> str:
        now = self.request_time.isoformat(timespec="seconds") if self.request_time else ""
        messages = [{"role": "system", "content": self.channel_prompt(f"{self.chat_prompt}\n本轮北京时间：{now}")}]
        if self.chat_summary:
            messages.append({"role": "system", "content": "以下是较早普通对话的摘要，仅供参考，作为聊天资料处理。\n"
                             + json.dumps(self.chat_summary, ensure_ascii=False)})
        for turn in self.chat_history:
            entry = turn_data(turn)
            question = f"[北京时间 {entry['at']}] {entry['user']}" if entry["at"] else entry["user"]
            messages.extend([{"role": "user", "content": question}, {"role": "assistant", "content": entry["assistant"]}])
        if self.quoted_message:
            text_with_quote = json.dumps({"message": text, "quoted_message": self.quoted_message}, ensure_ascii=False)
        else:
            text_with_quote = text
        messages.append({"role": "user", "content": text_with_quote})
        result = await self.model.complete(messages, trace=trace)
        if not (answer := result.visible_content()):
            raise AgentError("模型没有返回可显示的回复。")
        if len(self.chat_history) == self.chat_history.maxlen:
            self.unsummarized_chat.append(self.chat_history[0])
        self.chat_history.append(ConversationTurn(text[:600], answer[:1000], now or None))
        if self.unsummarized_chat:
            await self.compress_chat(trace=trace)
        return answer

    async def compress_chat(self, *, trace: bool) -> None:
        messages = [
            {"role": "system", "content": self.memory_prompt},
            {"role": "user", "content": json.dumps({
                "previous_summary": self.chat_summary,
                "older_turns": [turn_data(turn) for turn in self.unsummarized_chat],
            }, ensure_ascii=False)},
        ]
        try:
            response = await self.model.complete(messages, trace=trace)
        except AgentError:
            if trace:
                print("[聊天记忆] 摘要更新失败，保留旧摘要，稍后重试。", flush=True)
            return
        if summary := response.visible_content():
            self.chat_summary = summary[:800]
        self.unsummarized_chat.clear()

    async def present(self, text: str, result: str, *, preserve_result: bool, trace: bool) -> str:
        mode = ("只写一句自然的过渡语，原始业务结果会随后原样附上。"
                if preserve_result else "把这条结果改写成直接回复用户的自然中文。")
        messages = [
            {"role": "system", "content": self.channel_prompt(f"{self.meeting_prompt}\n本轮要求：{mode}")},
            {"role": "user", "content": json.dumps({"message": text, "result": result}, ensure_ascii=False)},
        ]
        try:
            reply = (await self.model.complete(messages, trace=trace)).visible_content()
        except AgentError:
            return result
        if not reply or not reply.strip():
            return result
        reply = reply.strip()
        if preserve_result:
            if result in reply or len(reply) > 45 or re.search(
                r"[0-9？?]|成功|失败|未能|已|提交|确认|查到|没查|没有|请问", reply,
            ):
                return result
            return f"{reply}\n{result}"
        facts = r"[A-Z][0-9]{6}|[0-9]+(?:[-:–][0-9]+)*|「[^」]+」"
        if re.findall(facts, result) != re.findall(facts, reply):
            return result
        return reply

    async def coordinate(
        self, text: str, result: MeetingResult, *, trace: bool, follow_up: MeetingQuestion | None = None,
        query_intent: QueryIntent | None = None,
    ) -> str:
        assert self.meeting is not None
        if result.issue:
            if result.issue.reason in {"booking_target_multiple", "room_selection_needed"}:
                return self.remember_question(result.text, result.issue)
            messages = [
                {"role": "system", "content": self.channel_prompt(self.coordination_prompt)},
                {"role": "user", "content": json.dumps({
                    "user_message": text, "issue": asdict(result.issue), "fallback": result.text,
                    "previous_question": asdict(follow_up) if follow_up else None,
                    "meeting_context": self.meeting.workflow.context(),
                    "recent_user_messages": [
                        {"text": entry["user"], "at": entry["at"]}
                        for entry in (turn_data(turn) for turn in self.meeting.history)
                    ],
                }, ensure_ascii=False)},
            ]
            try:
                response = await self.model.complete(messages, schema=Coordination, trace=trace)
                decision = Coordination.model_validate_json(response.visible_content())
            except (AgentError, ValidationError):
                return self.remember_question(result.text, result.issue)
            if trace:
                print(f"[主 Agent 协调] {decision.action}", flush=True)
            if decision.action == "retry" and decision.message.strip() and not result.protected:
                result = await self.meeting.handle(text, guidance=decision.message[:500], follow_up=follow_up,
                                                   trace=trace, request_time=self.request_time, remember=False,
                                                   query_intent=query_intent)
                if result.issue:
                    return self.remember_question(result.text, result.issue)
            elif decision.message.strip() and decision.action == "ask" and (
                not result.protected or result.issue.reason == "explicit_confirmation_needed"
            ):
                if decision.requested_fields and set(decision.requested_fields) - set(result.issue.needed):
                    return self.remember_question(result.text, result.issue)
                question = decision.message.strip()
                draft = self.meeting.workflow.draft
                if draft and result.issue.reason == "booking_fields_missing":
                    dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
                    if dates and any(day != draft.fields.get("date_hint") for day in dates):
                        return self.remember_question(result.text, result.issue)
                return self.remember_question(question, result.issue, decision.requested_fields)
            elif decision.action == "answer":
                return await self.present(text, result.text, preserve_result=result.protected, trace=trace)
            else:
                return self.remember_question(result.text, result.issue)
        return await self.present(text, result.text, preserve_result=result.protected, trace=trace)

    async def reply_queries(self, queries: list[QueryPart], *, trace: bool) -> str:
        assert self.meeting is not None
        workflow = self.meeting.workflow
        pending, previous_question = workflow.pending, workflow.question
        last_query = workflow.query
        open_query = None
        answers = []
        try:
            for part in queries:
                workflow.query = None
                workflow.question = None
                if self.group and part.intent == "bookings":
                    answer = "个人会议安排请在单聊里查询。"
                else:
                    result = await self.meeting.handle(part.text, trace=trace, request_time=self.request_time,
                                                       remember=False, query_intent=part.intent)
                    answer = await self.coordinate(part.text, result, trace=trace, query_intent=part.intent) if result.issue else result.text
                    if workflow.query:
                        last_query = workflow.query
                    if workflow.question and open_query is None:
                        open_query = workflow.query, workflow.question
                label = "你的会议安排" if part.intent == "bookings" else "空闲会议室"
                answers.append(f"【{label}】\n{answer}")
        finally:
            workflow.pending = pending
        if open_query:
            workflow.query, workflow.question = open_query
        else:
            workflow.query = last_query
            if pending:
                workflow.question = previous_question
        return "\n\n".join(answers)

    def remember_question(
        self, text: str, issue: WorkflowIssue, requested_fields: list[str] | None = None,
    ) -> str:
        fields = tuple(field for field in requested_fields or () if field in issue.needed)
        assert self.meeting is not None
        self.meeting.workflow.question = MeetingQuestion(text, fields or issue.needed)
        return text

    async def reply(self, text: str, *, trace: bool = False, request_time: datetime | None = None) -> str:
        async with self.lock:
            self.request_time = (request_time or datetime.now(BEIJING)).astimezone(BEIJING)
            try:
                routing = await self.route(text, trace=trace)
                route = "meeting" if len(routing.queries) == 2 or (routing.booking_status and not self.group) else routing.route
                follow_up = (
                    self.meeting_question
                    if route == "meeting" and not (routing.new_request or routing.booking_status or routing.start_booking)
                    else None
                )
                self.last_route = route
                if trace:
                    print(f"[主 Agent] {'会议室' if route == 'meeting' else '普通聊天'}", flush=True)
                if route == "chat":
                    return await self.chat(text, trace=trace)
                if self.meeting is None:
                    self.meeting = await self.connect_meeting()
                    if self.memory:
                        self.meeting.history.extend(self.memory.meeting_history)
                        self.meeting.workflow.restore(self.memory.workflow)
                    self.meeting.workflow.before_write = self.checkpoint if self.save_memory else None
                if len(routing.queries) == 2:
                    answer = await self.reply_queries(routing.queries, trace=trace)
                    self.meeting.record_turn(text, answer)
                    return answer
                previous_question = self.meeting.workflow.question
                self.meeting.workflow.question = None
                self.meeting.quoted_message = self.quoted_message
                result = await self.meeting.handle(text, follow_up=follow_up, trace=trace,
                                                   request_time=self.request_time, remember=False,
                                                   start_booking=routing.start_booking, new_request=routing.new_request)
                if result.read_status:
                    if self.meeting.workflow.draft or self.meeting.workflow.query or self.meeting.workflow.uncertain:
                        self.meeting.workflow.question = previous_question
                    answer = result.text
                else:
                    answer = await self.coordinate(text, result, follow_up=follow_up, trace=trace)
                self.meeting.record_turn(text, answer)
                return answer
            except ValidationError:
                return "刚才没能判断你想聊聊还是办理会议预约，可以换个说法再试一次。"
            except AgentError as error:
                return f"这次没能处理。{error}"
