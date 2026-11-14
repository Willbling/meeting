import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx

from api.models import BEIJING

from .main import ChatModel, MainAgent, MeetingAgent, ModelMessage


class WorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        start = (datetime.now(BEIJING) + timedelta(days=7)).replace(hour=14, minute=0, second=0, microsecond=0)
        self.times = {"starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat()}
        self.room = {"room_id": "R000001", "name": "三楼大会议室", "address": "301", "capacity": 12}
        self.booking = {
            **self.times, "booking_id": "A000010", "room_id": "R000001", "room_name": "三楼大会议室",
            "room_address": "301", "host_name": "林晓", "host_id": "P000001", "created_by": "P000001",
            "creator_name": "林晓", "subject": "评审", "status": "等待", "content": None, "attendees": [],
        }
        self.responses = []
        self.requests = []
        self.saved = False

        def complete(request):
            self.requests.append(json.loads(request.content))
            output = self.responses.pop(0)
            response = {"role": "assistant", "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)}
            return httpx.Response(200, json={"choices": [{"message": response}]})

        http = await self.enterAsyncContext(httpx.AsyncClient(
            base_url="http://test/v1/", transport=httpx.MockTransport(complete),
        ))
        self.agent = MeetingAgent(None, ChatModel(http, "qwen3.5:4b"), "P000001")
        self.mock_tool = AsyncMock(side_effect=self.tool)
        self.agent.workflow.call_tool = self.mock_tool

    async def tool(self, name, params):
        match name:
            case "find_available_rooms":
                return {"items": [self.room]}
            case "find_people":
                return {"items": [{"person_id": "P000003", "name": "陈宇"}]}
            case "list_my_bookings":
                return {"items": [self.booking] if self.saved else []}
            case "create_booking":
                self.saved = True
                return {"is_error": True, "code": "API_TIMEOUT", "message": "超时"}
            case "get_booking":
                return self.booking
            case "reschedule_booking":
                return {**self.booking, **params}
            case "cancel_booking":
                return {**self.booking, "status": "取消"}
        self.fail(f"Unexpected tool {name}")

    async def test_prepare_modify_confirm_and_reconcile(self) -> None:
        self.responses.extend([
            {"intent": "create", **self.times},
            {"intent": "continue", "subject": "旧主题"},
            {"intent": "continue", "subject": "评审"},
        ])
        self.assertIn("主题", await self.agent.reply("帮我订会议室"))
        self.mock_tool.assert_not_called()
        self.assertIn("旧主题", await self.agent.reply("主题是旧主题"))
        self.assertIn("「评审」", await self.agent.reply("改成评审"))
        self.assertFalse(self.saved)
        self.assertIn("无法确认", await self.agent.reply("确认"))
        self.assertIn("已预订", await self.agent.reply("再试一次"))
        self.assertIn("没有待确认", await self.agent.reply("确认"))
        writes = [call.args[1] for call in self.mock_tool.call_args_list if call.args[0] == "create_booking"]
        self.assertEqual([item["subject"] for item in writes], ["评审"])
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all("tools" not in request for request in self.requests))
        self.assertEqual(len(self.agent.history), 3)

    async def test_reschedule_and_cancel_require_matching_confirmation(self) -> None:
        self.responses.extend([
            {"intent": "reschedule", "booking_id": self.booking["booking_id"],
             "ends_at": (datetime.fromisoformat(self.times["ends_at"]) + timedelta(minutes=30)).isoformat()},
            {"intent": "cancel", "booking_id": self.booking["booking_id"]},
        ])
        self.assertIn("原安排", await self.agent.reply("延长半小时"))
        self.assertIn("操作和待确认申请不同", await self.agent.reply("确认取消"))
        self.assertIn("已改期", await self.agent.reply("确认改期"))
        self.assertIn("请核对取消申请", await self.agent.reply("取消刚才那一场"))
        self.assertNotIn("cancel_booking", [call.args[0] for call in self.mock_tool.call_args_list])
        self.assertIn("已取消", await self.agent.reply("确认取消"))

    async def test_booking_draft_survives_intervening_query(self) -> None:
        self.responses.append({"intent": "create", "subject": "评审", **self.times})
        self.assertIn("请核对预订申请", await self.agent.reply("先准备一场评审"))
        self.assertIn("没有待开始", await self.agent.reply("查我的预约"))
        self.assertIn("没有待确认", await self.agent.reply("确认"))
        self.assertIn("请核对预订申请", await self.agent.reply("继续申请"))
        self.assertFalse(self.saved)
        self.assertEqual(len(self.requests), 1)

    async def test_main_agent_chat_excludes_meeting_context_and_keeps_draft(self) -> None:
        connect = AsyncMock(return_value=self.agent)
        main = MainAgent(self.agent.model, connect)
        self.responses.extend([
            {"route": "chat"}, "你好！", {"route": "meeting"},
            {"intent": "create", "subject": "内部排期", **self.times},
            "你可以先看看这份安排。", {"route": "chat"}, "周末可以去公园走走。",
            {"route": "meeting"}, "这是刚才的安排。",
            {"route": "meeting"}, "可以查看预约记录。",
        ])
        self.assertEqual(await main.reply("你好"), "你好！")
        connect.assert_not_awaited()
        self.assertIn("内部排期", await main.reply("预订会议室"))
        application = self.agent.workflow.pending
        self.assertIsNotNone(application)
        self.assertEqual(await main.reply("周末去哪玩？"), "周末可以去公园走走。")
        self.assertIs(self.agent.workflow.pending, application)
        chat_request = self.requests[-1]
        self.assertEqual([entry["content"] for entry in chat_request["messages"][1:]], [
            "你好", "你好！", "周末去哪玩？",
        ])
        route_request = self.requests[-2]
        self.assertNotIn("内部排期", str(route_request))
        self.assertIn("内部排期", await main.reply("继续申请"))
        self.assertIn("提交结果暂时无法确认", await main.reply("确认"))
        self.assertTrue(all("tools" not in request for request in self.requests))
        connect.assert_awaited_once()

    async def test_chat_compacts_old_turns_without_storing_reasoning(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        self.responses.extend([
            *[response for n in range(1, 5) for response in (
                {"route": "chat"}, f"<think>private chain {n}</think>回答 {n}",
            )],
            {"route": "chat"}, "回答 5", "用户偏好短答。",
            {"route": "chat"}, "回答 6", "用户偏好短答，正在讨论写作。",
        ])
        for n in range(1, 7):
            self.assertEqual(await main.reply(f"普通问题 {n}"), f"回答 {n}")
        self.assertEqual(list(main.chat_history), [(f"普通问题 {n}", f"回答 {n}") for n in range(3, 7)])
        self.assertEqual(main.chat_summary, "用户偏好短答，正在讨论写作。")
        summary_request = json.loads(self.requests[-1]["messages"][-1]["content"])
        self.assertEqual(summary_request, {
            "previous_summary": "用户偏好短答。",
            "older_turns": [{"user": "普通问题 2", "assistant": "回答 2"}],
        })
        available = [entry["content"] for entry in self.requests[-2]["messages"]]
        self.assertNotIn("普通问题 1", available)
        self.assertIn("普通问题 2", available)
        self.assertIn("用户偏好短答。", available[1])
        self.assertNotIn("private chain", str(self.requests))
        parsed = ModelMessage.model_validate({"role": "assistant", "content": "可见",
                                              "reasoning_content": "private", "thinking": "private"})
        self.assertEqual(parsed.model_dump(), {"role": "assistant", "content": "可见"})

    async def test_write_result_survives_model_rewording_failure(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        self.responses.extend([
            {"route": "meeting"}, {"intent": "cancel", "booking_id": "A000010"}, "请核对这一项。",
            {"route": "meeting"}, "已经失败，申请编号 A999999。",
        ])
        self.assertIn("请核对取消申请", await main.reply("取消 A000010"))
        reply = await main.reply("确认取消")
        self.assertIn("已取消", reply)
        self.assertIn("A000010", reply)
        self.assertNotIn("A999999", reply)

    async def test_tomorrow_schedule_queries_the_whole_day(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        self.responses.extend([{"route": "meeting"}, {"intent": "bookings", "date_hint": "明天"}, ""])
        reply = await main.reply("查查我明天的会议室安排")
        self.assertIn("没有查到", reply)
        tool, params = self.mock_tool.call_args.args
        tomorrow = datetime.now(BEIJING).date() + timedelta(days=1)
        self.assertEqual(tool, "list_my_bookings")
        self.assertEqual(params["starts_at"], f"{tomorrow}T00:00:00+08:00")
        self.assertEqual(params["ends_at"], f"{tomorrow + timedelta(days=1)}T00:00:00+08:00")

    async def test_today_overrides_model_repeating_tomorrows_bounds(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        today = datetime.now(BEIJING).date()
        tomorrow = today + timedelta(days=1)
        stale = {
            "intent": "bookings", "date_hint": str(tomorrow),
            "starts_at": f"{tomorrow}T00:00:00+08:00",
            "ends_at": f"{tomorrow + timedelta(days=1)}T00:00:00+08:00",
        }
        self.responses.extend([
            {"route": "meeting"}, stale, "",
            {"route": "meeting"}, stale, "",
        ])
        self.assertIn(str(tomorrow), await main.reply("查一下我明天的会议安排"))
        self.assertIn(str(today), await main.reply("我今天的工作安排呢"))
        searches = [call.args[1] for call in self.mock_tool.call_args_list]
        self.assertEqual([params["starts_at"] for params in searches], [
            f"{tomorrow}T00:00:00+08:00", f"{today}T00:00:00+08:00",
        ])
        self.assertEqual(searches[1]["ends_at"], f"{tomorrow}T00:00:00+08:00")

    async def test_room_queries_anchor_tomorrow_and_preserve_explicit_hours(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        today = datetime.now(BEIJING).date()
        tomorrow = today + timedelta(days=1)
        stale = {
            "intent": "rooms", "date_hint": str(today), "day_part": "afternoon",
            "starts_at": f"{today}T14:00:00+08:00", "ends_at": f"{today}T15:00:00+08:00",
        }
        self.responses.extend([
            {"route": "meeting"}, {"intent": "bookings", "date_hint": "明天"}, "",
            {"route": "meeting"}, stale, "",
            {"route": "meeting"}, stale, "",
        ])
        self.assertIn(str(tomorrow), await main.reply("我明天有会议安排吗"))
        reply = await main.reply("明天下午有空闲的会议室吗")
        self.assertIn(f"{tomorrow:%m-%d} 13:00–18:00", reply)
        tool, params = self.mock_tool.call_args.args
        self.assertEqual(tool, "find_available_rooms")
        self.assertEqual(params["starts_at"], f"{tomorrow}T13:00:00+08:00")
        self.assertEqual(params["ends_at"], f"{tomorrow}T18:00:00+08:00")
        self.assertEqual(params["mode"], "intervals")

        await main.reply("查明天下午2点到3点空闲的会议室")
        _, params = self.mock_tool.call_args.args
        self.assertEqual(params["starts_at"], f"{tomorrow}T14:00:00+08:00")
        self.assertEqual(params["ends_at"], f"{tomorrow}T15:00:00+08:00")
        self.assertEqual(params.get("mode", "full"), "full")

    async def test_main_agent_retries_with_guidance_from_original_request(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        self.responses.extend([
            {"route": "meeting"}, {"intent": "create", **self.times},
            {"action": "retry", "message": "用户把主题明确说成「评审」。请补到 subject。"},
            {"intent": "continue", "subject": "评审"}, "",
        ])
        self.assertIn("请核对预订申请", await main.reply("订个会议室开评审"))
        self.assertIn("主助手的解析建议", self.requests[-2]["messages"][-1]["content"])
        self.assertFalse(self.saved)
        self.assertEqual(len(self.agent.history), 1)

    async def test_main_agent_asks_only_for_missing_information(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        self.responses.extend([
            {"route": "meeting"}, {"intent": "create", **self.times},
            {"action": "ask", "message": "时段已经有了。这场会叫什么？"},
        ])
        self.assertEqual(await main.reply("帮我订一下这个时间"), "时段已经有了。这场会叫什么？")
        self.assertIn("booking_fields_missing", self.requests[-1]["messages"][-1]["content"])
        self.mock_tool.assert_not_called()

    async def test_short_title_answers_the_actual_question(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        day = self.times["starts_at"][:10]
        self.responses.extend([
            {"route": "meeting"},
            {"host": "", "members": ["我", "陈宇"], "date_hint": day},
            {"action": "ask", "message": "请问明天的会议主题是什么？", "requested_fields": ["subject"]},
            {"route": "meeting"}, {"subject": "降雨预测阶段总结"},
            {"action": "ask", "message": "明天下午几点开始？", "requested_fields": ["starts_at"]},
            {"route": "meeting"}, {"starts_at": self.times["starts_at"]}, "",
        ])
        question = await main.reply("我需要预定会议室，明天下午2个小时，我和陈宇的")
        self.assertEqual(question, "请问明天的会议主题是什么？")
        self.assertEqual(await main.reply("降雨预测阶段总结"), "明天下午几点开始？")
        route_context = json.loads(self.requests[3]["messages"][-1]["content"])
        self.assertEqual(route_context["previous_meeting_question"], {"text": question, "fields": ["subject"]})
        self.assertIn(question, self.requests[4]["messages"][-1]["content"])
        self.assertIn("降雨预测阶段总结", await main.reply("三点"))
        arguments = self.agent.workflow.pending.arguments
        self.assertEqual(arguments["ends_at"], (datetime.fromisoformat(arguments["starts_at"]) + timedelta(hours=2)).isoformat())
        self.assertEqual(arguments["member_ids"], ["P000003"])
        self.assertFalse(self.saved)

    async def test_flexible_search_window_is_not_a_reservation(self) -> None:
        day = datetime.now(BEIJING).date() + timedelta(days=1)
        self.times = {"starts_at": f"{day}T14:00:00+08:00", "ends_at": f"{day}T15:00:00+08:00"}
        start = self.times["starts_at"]
        self.responses.extend([
            {"intent": "rooms", "date_hint": start[:10], "day_part": "afternoon"},
            {"intent": "create", "room_name": self.room["name"], "subject": "评审", "attendee_count": 2},
            {"intent": "continue", "members": ["陈宇"]},
            {"intent": "continue", **self.times},
            {"intent": "confirm"},
        ])
        self.assertIn(self.room["name"], await self.agent.reply("看看明天下午空闲的会议室"))
        _, params = self.mock_tool.call_args.args
        self.assertEqual(params["mode"], "intervals")
        self.assertTrue(params["starts_at"].endswith("T13:00:00+08:00"))
        self.assertIn("名单", await self.agent.reply("三楼大会议室，我和陈宇两个人的会议"))
        self.assertIn("起止时间", await self.agent.reply("陈宇"))
        self.assertIn("陈宇", await self.agent.reply("下午2点到3点"))
        self.assertIn("补充", await self.agent.reply("好的就这么预定行，其他与"))
        self.assertFalse(self.saved)
        self.assertIsNotNone(self.agent.workflow.pending)

    async def test_edit_recent_booking_content_and_add_member(self) -> None:
        self.agent.workflow.last_written_booking = self.booking["booking_id"]
        self.responses.append({
            "intent": "continue", "content": "9月份工作总结与10月安排",
            "members": ["陈宇"], "member_mode": "add",
        })
        summary = await self.agent.reply("补充一下刚刚预定的会议内容，9月份工作总结与10月安排，再加陈宇")
        self.assertIn("请核对修改申请", summary)
        self.assertIn("未填写 → 9月份工作总结与10月安排", summary)
        self.assertNotIn("reschedule_booking", [call.args[0] for call in self.mock_tool.call_args_list])
        self.assertIn("已修改", await self.agent.reply("确认修改"))
        self.assertEqual(self.mock_tool.call_args.args[1], {
            "booking_id": "A000010", "content": "9月份工作总结与10月安排", "member_ids": ["P000003"],
        })

    async def test_edit_tomorrows_only_booking_after_showing_schedule(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        booking = {**self.booking, "subject": "光伏预测项目总结"}
        self.mock_tool.side_effect = [{"items": [booking]}, {"items": [booking]}, booking]
        self.responses.extend([
            {"route": "meeting"}, {"intent": "bookings", "date_hint": "明天"}, "",
            {"route": "meeting"}, {"intent": "reschedule"}, "",
        ])
        self.assertIn("光伏预测项目总结", await main.reply("看看我明天的会议安排"))
        memory = self.agent.workflow.booking_search
        self.assertTrue(memory.complete)
        reply = await main.reply('给明天的会议安排加上会议内容，"9月工作总结和10月安排"')
        self.assertIn("请核对修改申请", reply)
        self.assertEqual(self.agent.workflow.pending.arguments, {
            "booking_id": booking["booking_id"], "content": "9月工作总结和10月安排",
        })
        lookup = self.mock_tool.call_args_list[1].args[1]
        self.assertEqual(lookup["starts_at"], memory.scope["starts_at"])
        self.assertEqual(lookup["ends_at"], memory.scope["ends_at"])
        self.assertEqual(lookup["status"], "等待")

    async def test_edit_this_booking_reuses_scope_and_reports_failed_write(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        self.mock_tool.side_effect = [
            {"items": [self.booking]}, {"items": [self.booking]}, self.booking, self.booking,
            {"is_error": True, "code": "DATABASE_BUSY", "http_status": 503, "message": "数据库正被占用"},
        ]
        self.responses.extend([
            {"route": "meeting"}, {"intent": "bookings", "date_hint": "明天"}, "",
            {"route": "meeting"}, {"intent": "reschedule", "date_hint": "明天", **self.times}, "",
            {"route": "meeting"}, "",
        ])
        await main.reply("看看我明天的会议安排")
        reply = await main.reply("给这个会议安排加上会议内容，“9月份工作总结与10月份工作安排”")
        self.assertIn("请核对修改申请", reply)
        self.assertEqual(self.agent.workflow.pending.arguments, {
            "booking_id": self.booking["booking_id"], "content": "9月份工作总结与10月份工作安排",
        })
        reply = await main.reply("确认")
        self.assertIn("这次没有保存", reply)
        self.assertIn("数据库正被占用", reply)
        self.assertIsNotNone(self.agent.workflow.pending)
        self.assertIsNone(self.agent.workflow.uncertain)

    async def test_edit_multiple_bookings_resolves_abbreviated_target(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        target = {**self.booking, "subject": "光伏预测项目总结"}
        other = {**self.booking, "subject": "费用评审", "booking_id": "A000011"}
        self.mock_tool.side_effect = [
            {"items": [target, other]}, {"items": [target, other]}, {"items": [target, other]}, target,
        ]
        self.responses.extend([
            {"route": "meeting"}, {"intent": "bookings", "date_hint": "明天"}, "",
            {"route": "meeting"}, {"intent": "reschedule", "use_booking_search": True,
                                   "content": "9月工作总结和10月安排"},
            {"route": "meeting"}, {}, "",
        ])
        await main.reply("看看我明天的会议安排")
        reply = await main.reply("给刚查到的会议加上会议内容")
        self.assertIn("A000010", reply)
        self.assertIn("A000011", reply)
        self.assertIsNone(self.agent.workflow.pending)
        self.assertEqual(main.meeting_question.fields, ("booking_target",))
        reply = await main.reply("光伏的项目")
        self.assertIn("请核对修改申请", reply)
        self.assertEqual(self.agent.workflow.pending.arguments, {
            "booking_id": target["booking_id"], "content": "9月工作总结和10月安排",
        })
        target_properties = self.requests[-2]["response_format"]["json_schema"]["schema"]["properties"]
        self.assertNotIn("content", target_properties)
        self.assertNotIn("subject", target_properties)

    async def test_move_listed_booking_keeps_original_hours_when_model_uses_wrong_target_date(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        original = datetime.fromisoformat(self.booking["starts_at"])
        new_day = original.date() + timedelta(days=1)
        self.mock_tool.side_effect = [{"items": [self.booking]}, self.booking]
        self.responses.extend([
            {"route": "meeting"}, {"intent": "bookings", "date_hint": str(original.date())}, "",
            {"route": "meeting"}, {"intent": "reschedule", "target_date_hint": str(new_day)}, "",
        ])
        await main.reply("查我的会议安排")
        answer = await main.reply(f"改成{new_day.month}月{new_day.day}日同样时间点的")
        self.assertIn("请核对改期申请", answer)
        self.assertEqual(self.agent.workflow.pending.arguments, {
            "booking_id": self.booking["booking_id"],
            "starts_at": (original + timedelta(days=1)).isoformat(),
            "ends_at": (datetime.fromisoformat(self.booking["ends_at"]) + timedelta(days=1)).isoformat(),
        })
        self.assertEqual([call.args[0] for call in self.mock_tool.call_args_list], ["list_my_bookings", "get_booking"])

    async def test_move_requires_selection_when_several_listed_bookings_match(self) -> None:
        main = MainAgent(self.agent.model, AsyncMock(return_value=self.agent))
        original = datetime.fromisoformat(self.booking["starts_at"])
        second = {**self.booking, "booking_id": "A000011", "subject": "另一场会议"}
        bookings = {"items": [self.booking, second]}
        self.mock_tool.side_effect = [bookings, bookings, second]
        self.responses.extend([
            {"route": "meeting"}, {"intent": "bookings", "date_hint": str(original.date())}, "",
            {"route": "meeting"}, {"intent": "continue", "target_date_hint": "明天"},
            {"route": "meeting"}, {"selection": 2}, "",
        ])
        await main.reply("查我的会议安排")
        answer = await main.reply("改成明天同样时间点的")
        self.assertIn("请选一下", answer)
        self.assertIsNone(self.agent.workflow.pending)
        self.assertIn("请核对改期申请", await main.reply("第二场"))
        self.assertEqual(self.agent.workflow.pending.arguments["booking_id"], second["booking_id"])
