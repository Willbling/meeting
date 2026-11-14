import asyncio
import os
import socket
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from mcp import Client, StdioServerParameters

from data.sqlite.init_db import initialize
from mcp_server.client import ACTOR_META_KEY


PROJECT = Path(__file__).resolve().parents[1]


class MeetingMcpTest(unittest.IsolatedAsyncioTestCase):
    def local_client(self, api_url: str) -> Client:
        return Client(StdioServerParameters(
            command=sys.executable, args=["-B", "-m", "mcp_server.server", "--transport", "stdio"], cwd=PROJECT,
            env={"MEETING_API_URL": api_url},
        ))

    async def test_stdio_booking_lifecycle(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        database = Path(directory) / "test.db"
        initialize(database)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        api_url = f"http://127.0.0.1:{port}"
        with subprocess.Popen(
            [sys.executable, "-B", "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=PROJECT, env={**os.environ, "MEETING_DB_PATH": str(database)},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        ) as server:
            try:
                async with httpx.AsyncClient(base_url=api_url, trust_env=False, timeout=0.5) as http:
                    for _ in range(40):
                        if server.poll() is not None:
                            self.fail(server.stderr.read().decode(errors="replace"))
                        try:
                            (await http.get("/readyz")).raise_for_status()
                            break
                        except httpx.TransportError:
                            await asyncio.sleep(0.2)
                    else:
                        self.fail("FastAPI did not start")
                await asyncio.wait_for(self.check_tools(api_url), timeout=30)
            finally:
                server.terminate()

    async def check_tools(self, api_url: str) -> None:
        async with self.local_client(api_url) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertEqual(set(tools), {
                "find_people", "find_available_rooms", "list_my_bookings", "get_booking",
                "create_booking", "reschedule_booking", "cancel_booking",
            })
            self.assertTrue(all(tool.output_schema for tool in tools.values()))
            self.assertTrue(all(tool.description for tool in tools.values()))
            for tool in tools.values():
                self.assertTrue(all(field.get("description") for field in tool.input_schema["properties"].values()))
            self.assertNotIn("person_id", tools["create_booking"].input_schema["properties"])
            self.assertNotIn("P000001", client.instructions or "")
            missing = await client.call_tool("list_my_bookings")
            self.assertEqual(missing.structured_content["code"], "ACTOR_REQUIRED")
            invalid_actor = await client.call_tool("list_my_bookings", meta={ACTOR_META_KEY: "invalid"})
            self.assertEqual(invalid_actor.structured_content["code"], "INVALID_ACTOR")

            creator, host, member = "P000001", "P000002", "P000003"

            async def call(actor: str, tool_name: str, **params) -> dict:
                response = await client.call_tool(tool_name, params, meta={ACTOR_META_KEY: actor})
                self.assertFalse(response.is_error, response.content)
                self.assertIsNotNone(response.structured_content)
                return response.structured_content

            person = (await call(creator, "find_people", name="周宁"))["items"][0]
            start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=7)
            window = {"starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat()}
            rooms = (await call(creator, "find_available_rooms", min_capacity=8, **window))["items"]
            self.assertEqual([room["room_id"] for room in rooms], ["R000001"])
            public_rooms = await client.call_tool("find_available_rooms", {"min_capacity": 8, **window})
            self.assertFalse(public_rooms.is_error)
            self.assertEqual(public_rooms.structured_content["items"], rooms)
            reservation = {
                **window, "room_id": rooms[0]["room_id"], "host_id": person["person_id"],
                "subject": "MCP 联调会议", "member_ids": ["P000003"],
            }
            booking = await call(creator, "create_booking", **reservation)
            booking_id = booking["booking_id"]
            ranges = await call(creator, "find_available_rooms", mode="intervals",
                                starts_at=(start - timedelta(minutes=20)).isoformat(), ends_at=window["ends_at"])
            partial = next(room for room in ranges["items"] if room["room_id"] == "R000001")
            self.assertEqual(len(partial["free_intervals"]), 1)
            self.assertEqual(len(partial["busy_intervals"]), 1)
            edited = await call(host, "reschedule_booking", booking_id=booking_id, subject="新主题", content="内容")
            self.assertEqual(edited["subject"], "新主题")
            cleared = await call(host, "reschedule_booking", booking_id=booking_id, clear_content=True)
            self.assertIsNone(cleared["content"])
            self.assertEqual(booking["created_by"], "P000001")
            self.assertEqual(booking["host_id"], "P000002")
            self.assertEqual(booking["room_name"], "三楼大会议室")
            self.assertTrue(booking["room_address"])
            self.assertTrue(booking["starts_at"].endswith("+08:00"))
            details = await call(member, "get_booking", booking_id=booking_id)
            self.assertEqual([person["role"] for person in details["attendees"]], ["主持人", "成员"])
            bookings = await call(host, "list_my_bookings", status="等待", **window)
            self.assertEqual([item["booking_id"] for item in bookings["items"]], [booking_id])
            creator_bookings, host_bookings = await asyncio.gather(
                call(creator, "list_my_bookings"), call(host, "list_my_bookings"),
            )
            creator_ids = {item["booking_id"] for item in creator_bookings["items"]}
            host_ids = {item["booking_id"] for item in host_bookings["items"]}
            self.assertIn("A000007", creator_ids - host_ids)
            self.assertIn("A000006", host_ids - creator_ids)

            duplicate = await client.call_tool("create_booking", reservation, meta={ACTOR_META_KEY: host})
            self.assertTrue(duplicate.is_error)
            self.assertEqual(duplicate.structured_content["code"], "ROOM_TIME_CONFLICT")
            self.assertIn("ROOM_TIME_CONFLICT", duplicate.content[0].text)
            forbidden = await client.call_tool("cancel_booking", {"booking_id": booking_id}, meta={ACTOR_META_KEY: member})
            self.assertTrue(forbidden.is_error)
            self.assertEqual(forbidden.structured_content["code"], "BOOKING_FORBIDDEN")
            invalid = await client.call_tool("reschedule_booking", {"booking_id": booking_id}, meta={ACTOR_META_KEY: host})
            self.assertTrue(invalid.is_error)
            self.assertEqual(invalid.structured_content["code"], "VALIDATION_ERROR")

            moved = await call(host, "reschedule_booking", booking_id=booking_id, room_id="R000002")
            self.assertEqual(moved["room_id"], "R000002")
            canceled = await call(creator, "cancel_booking", booking_id=booking_id)
            self.assertEqual(canceled["status"], "取消")
            self.assertEqual(await call(host, "cancel_booking", booking_id=booking_id), canceled)
            replacement = await call(creator, "create_booking", **{**reservation, "room_id": "R000002"})
            self.assertNotEqual(replacement["booking_id"], booking_id)
