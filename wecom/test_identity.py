import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock

from agent.main import AgentMemory, MainAgent, MeetingAgent, ModelMessage
from agent.workflow import Application, Interpretation, WorkflowMemory
from api.models import BEIJING
from data.sqlite.init_db import initialize
from mcp_server.client import result

from . import memory
from .main import bind_user, message_reply


class WeComIdentityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.database = root / "test.db"
        self.memory_dir = root / "memory"
        initialize(self.database)
        bind_user(self.database, "P000001", "ZhangYueHui")
        bind_user(self.database, "P000002", "BuLuLu")
        self.responses = []
        self.requests = []
        self.model = Mock(complete=AsyncMock(side_effect=self.complete))
        self.model.interpret = AsyncMock(side_effect=self.interpret)
        self.tool = AsyncMock(return_value=result({"items": []}))
        self.create_agent = Mock(side_effect=self.make_agent)

    async def complete(self, messages, **kwargs):
        self.requests.append(messages)
        output = self.responses.pop(0)
        return ModelMessage(role="assistant", content=output if isinstance(output, str) else json.dumps(output))

    async def interpret(self, messages, **kwargs):
        return Interpretation.model_validate_json((await self.complete(messages)).visible_content())

    def make_agent(self, person_id, *, group=False):
        meeting = MeetingAgent(None, self.model, person_id, group=group)

        async def call(name, arguments):
            response = await self.tool(name, arguments)
            return response.structured_content

        meeting.workflow.call_tool = call
        agent = MainAgent(self.model, AsyncMock(return_value=meeting))
        agent.group = group
        return agent

    async def send(self, user, text, chat=None):
        return await message_reply({
            "chattype": "group" if chat else "single", "chatid": chat,
            "from": {"userid": user}, "text": {"content": text},
        }, self.database, self.create_agent, self.memory_dir)

    async def test_sender_binding_and_saved_chat(self) -> None:
        self.assertIn("联系管理员", await self.send("unbound", "查看我的预约"))
        self.assertIn("unbound", await self.send("unbound", "我的账号"))
        self.create_agent.assert_not_called()

        self.responses.extend([
            {"route": "chat"}, "记住了", {"route": "chat"}, "你好",
            {"route": "chat"}, "当然记得",
        ])
        await self.send("ZhangYueHui", "我在写报告")
        await self.send("BuLuLu", "你好")
        self.assertNotIn("我在写报告", str(self.requests[-1]))
        await self.send("ZhangYueHui", "还记得吗")
        self.assertIn("我在写报告", str(self.requests[-1]))
        self.assertEqual([call.args for call in self.create_agent.call_args_list], [
            ("P000001",), ("P000002",), ("P000001",),
        ])
        self.assertEqual(len(list((self.memory_dir / "single").glob("*.txt"))), 2)

        bind_user(self.database, "P000001", "new_user")
        self.assertIn("联系管理员", await self.send("ZhangYueHui", "确认"))

    async def test_group_can_resume_after_recreating_agent_and_keeps_context_separate(self) -> None:
        day = datetime.now(BEIJING).date() + timedelta(days=1)
        self.responses.extend([
            {"route": "meeting"}, {"intent": "rooms", "date_hint": str(day)},
            {"action": "ask", "message": "明天几点方便？"},
            {"route": "meeting"},
            {"starts_at": f"{day}T14:00:00+08:00", "ends_at": f"{day}T15:00:00+08:00"}, "",
            {"route": "chat"}, "你好", {"route": "chat"}, "你好",
        ])
        self.assertEqual(await self.send("unbound", "@会议室助手 明天找间能开会的屋子", "group1"), "明天几点方便？")
        follow_up = await self.send("unbound", "@会议室助手 下午两点到三点", "group1")
        self.assertIn(f"{day:%m-%d} 14:00–15:00", follow_up)
        self.assertEqual(self.tool.await_args.args[0], "find_available_rooms")
        self.assertIn("明天几点方便？", str(self.requests[3]))
        await self.send("other", "@会议室助手 你好", "group1")
        self.assertNotIn("明天几点方便", str(self.requests[-2]))
        await self.send("unbound", "@会议室助手 你好", "group2")
        self.assertNotIn("明天几点方便", str(self.requests[-2]))
        self.assertEqual(len(list((self.memory_dir / "groups").glob("*/*.txt"))), 3)

    async def test_group_cannot_query_personal_bookings(self) -> None:
        self.responses.extend([{"route": "meeting"}, {"intent": "bookings"}, ""])
        self.assertIn("单聊", await self.send("ZhangYueHui", "@会议室助手 查我的预约", "group1"))
        self.tool.assert_not_awaited()

    async def test_confirmation_checkpoints_before_writing(self) -> None:
        user = "ZhangYueHui"
        path = self.memory_dir / "single" / f"{memory.file_key(user)}.txt"
        original = {
            "booking_id": "A000009", "subject": "测试", "room_name": "二楼讨论室", "room_address": "205",
            "starts_at": "2026-11-15T14:00:00+08:00", "ends_at": "2026-11-15T15:00:00+08:00",
        }
        pending = Application("cancel", {"booking_id": "A000009"}, "测试", original)
        memory.save(path, "P000001", AgentMemory(last_route="meeting", workflow=WorkflowMemory(pending=pending)))

        async def call(name, arguments):
            if name == "get_booking":
                return result(original)
            self.assertEqual(name, "cancel_booking")
            snapshot = memory.load(path, "P000001").workflow
            self.assertIsNone(snapshot.pending)
            self.assertEqual(snapshot.uncertain, pending)
            return result({**original, "status": "取消"})

        self.tool.side_effect = call
        self.responses.extend([{"route": "meeting"}, ""])
        self.assertIn("已取消", await self.send(user, "确认"))
        snapshot = memory.load(path, "P000001").workflow
        self.assertIsNone(snapshot.uncertain)
        self.assertEqual(snapshot.last_written_booking, "A000009")

    def test_reset_and_typed_memory(self) -> None:
        path = self.memory_dir / "single" / f"{memory.file_key('id')}.txt"
        pending = Application("cancel", {"booking_id": "A000009"}, "测试", known_ids=frozenset({"A000009"}))
        memory.save(path, "P000001", AgentMemory(workflow=WorkflowMemory(uncertain=pending)))
        self.assertEqual(memory.load(path, "P000001").workflow.uncertain, pending)
        self.assertIsNone(memory.load(path, "P000002"))
        unrelated = path.parent / "keep.txt"
        unrelated.write_text("保留", encoding="utf-8")
        group_path = self.memory_dir / "groups" / memory.file_key("group") / path.name
        memory.save(group_path, None, AgentMemory())
        memory.reset(self.memory_dir)
        self.assertFalse(path.exists())
        self.assertFalse(group_path.exists())
        self.assertTrue(unrelated.exists())
