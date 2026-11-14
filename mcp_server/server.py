import argparse
import os
from pathlib import Path
from typing import Annotated

import httpx
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from api.models import (
    ApiDatetime, AvailabilityMode, Booking, BookingDetail, BookingId, BookingStatus,
    Person, PersonId, Room, RoomAvailability, RoomId, Subject,
)
from service_config import DEFAULT_CONFIG, test_person_id, validate_person_id

from .client import ACTOR_META_KEY, MeetingApi, api_error


Limit = Annotated[int, Field(ge=1, le=100, description="每页返回条数，默认 50，最多 100。")]
Offset = Annotated[int, Field(ge=0, le=2**63 - 1, description="跳过的记录条数，默认 0。")]
Time = Annotated[ApiDatetime, Field(description="带时区的整秒日期时间，例如 2026-11-16T14:00:00+08:00")]
BookingNumber = Annotated[BookingId, Field(description="申请编号，例如 A000001，从预约查询结果取得。")]
RoomNumber = Annotated[RoomId, Field(description="会议室编号，例如 R000001，从查房结果取得。")]


class SearchResults[T](BaseModel):
    items: list[T]


def create_server(api_url: str) -> MCPServer:
    server = MCPServer("meeting-room-booking", instructions="会议室预订工具。调用方随每次工具请求提供当前操作者。")
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    async def call_api(ctx: Context, method: str, path: str, model: type[BaseModel], **options) -> CallToolResult:
        person_id = (ctx.request_context.meta or {}).get(ACTOR_META_KEY)
        if person_id is None:
            if method == "GET" and path == "/rooms":
                return await MeetingApi(api_url, None).call(method, path, model, **options)
            return api_error("ACTOR_REQUIRED", "工具调用缺少操作者身份")
        try:
            actor = validate_person_id(person_id)
        except ValidationError:
            return api_error("INVALID_ACTOR", "操作者编号格式有误")
        return await MeetingApi(api_url, actor).call(method, path, model, **options)

    @server.tool(name="find_people", description="按姓名片段查人员，返回姓名、人物编号、职务和职级。同名时请让用户确认人选。", annotations=read_only)
    async def find_people(
        ctx: Context,
        name: Annotated[str, Field(min_length=1, pattern=r"\S", description="姓名或连续的姓名片段，例如周宁。")],
        limit: Limit = 50, offset: Offset = 0,
    ) -> Annotated[CallToolResult, SearchResults[Person]]:
        return await call_api(ctx, "GET", "/people", SearchResults[Person], list_result=True,
                              params={"name": name, "limit": limit, "offset": offset})

    @server.tool(name="find_available_rooms", description="查可用会议室。full 要求全程空闲；intervals 返回查询范围内的空闲和占用时段，支持最短连续空闲时长。", annotations=read_only)
    async def find_available_rooms(
        ctx: Context,
        starts_at: Annotated[Time, Field(description="查询开始时间，带时区的整秒 ISO 8601 日期时间。")],
        ends_at: Annotated[Time, Field(description="查询结束时间，带时区的整秒 ISO 8601 日期时间。")],
        min_capacity: Annotated[int, Field(ge=1, le=2**63 - 1, description="最低座位数，须计入主持人。")] = 1,
        name: Annotated[str | None, Field(description="可选的会议室名称片段。")] = None,
        limit: Limit = 50, offset: Offset = 0,
        mode: Annotated[AvailabilityMode, Field(description="full 全程空闲；intervals 区间有空。")] = "full",
        min_duration_minutes: Annotated[int | None, Field(ge=1, le=10080, description="区间模式要求的最短连续空闲分钟数。")] = None,
    ) -> Annotated[CallToolResult, SearchResults[RoomAvailability | Room]]:
        model = SearchResults[RoomAvailability] if mode == "intervals" else SearchResults[Room]
        return await call_api(ctx, "GET", "/rooms", model, list_result=True, params={
            "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat(), "min_capacity": min_capacity,
            "name": name, "limit": limit, "offset": offset,
            "mode": mode, "min_duration_minutes": min_duration_minutes,
        })

    @server.tool(name="list_my_bookings", description="查当前操作者创建、主持或参加的预约。时段筛选匹配相交的预约，结果包含会议室名、地址、主持人和预订人。", annotations=read_only)
    async def list_my_bookings(
        ctx: Context,
        room_id: Annotated[RoomNumber | None, Field(description="可选的会议室编号，例如 R000001。")] = None,
        status: Annotated[BookingStatus | None, Field(description="可选的预约状态：等待、会议中、取消或结束。")] = None,
        starts_at: Annotated[Time | None, Field(description="可选的筛选起点，匹配在此时刻之后结束的预约。使用带时区的整秒时间。")] = None,
        ends_at: Annotated[Time | None, Field(description="可选的筛选终点，匹配在此时刻之前开始的预约。使用带时区的整秒时间。")] = None,
        limit: Limit = 50, offset: Offset = 0,
    ) -> Annotated[CallToolResult, SearchResults[Booking]]:
        return await call_api(ctx, "GET", "/bookings", SearchResults[Booking], list_result=True, params={
            "room_id": room_id, "status": status, "starts_at": starts_at.isoformat() if starts_at else None,
            "ends_at": ends_at.isoformat() if ends_at else None, "limit": limit, "offset": offset,
        })

    @server.tool(name="get_booking", description="按申请编号查预约详情，包括北京时间、房间名称和地址、会议内容、预订人、主持人与成员。只有创建人、主持人或成员可查看。", annotations=read_only)
    async def get_booking(ctx: Context, booking_id: BookingNumber) -> Annotated[CallToolResult, BookingDetail]:
        return await call_api(ctx, "GET", f"/bookings/{booking_id}", BookingDetail)

    @server.tool(
        name="create_booking",
        description="为未来时段创建预约。可指定同事为主持人以代订，预订人始终是当前操作者。成功后返回申请编号和完整预约。",
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False, open_world_hint=False),
    )
    async def create_booking(
        ctx: Context,
        room_id: RoomNumber,
        host_id: Annotated[PersonId, Field(description="主持人人物编号，例如 P000002，可通过查人取得。")],
        starts_at: Annotated[Time, Field(description="未来的开始时间，带时区的整秒 ISO 8601 日期时间。")],
        ends_at: Annotated[Time, Field(description="晚于开始时间的结束时间，带时区的整秒 ISO 8601 日期时间。")],
        subject: Annotated[Subject, Field(description="必填的非空会议主题。")],
        member_ids: Annotated[list[PersonId] | None, Field(description="其他与会人员编号。省略表示没有成员，编号须唯一且不能包含主持人。")] = None,
        content: Annotated[str | None, Field(description="可选的会议内容或议程。")] = None,
    ) -> Annotated[CallToolResult, BookingDetail]:
        return await call_api(ctx, "POST", "/bookings", BookingDetail, body={
            "room_id": room_id, "host_id": host_id, "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(), "subject": subject, "member_ids": member_ids or [], "content": content,
        })

    @server.tool(
        name="reschedule_booking",
        description="编辑等待中的预约。可修改时间、会议室、主题、内容和其他与会人员。仅创建人或主持人可操作；省略字段保留原值；冲突时整笔修改撤销。",
        annotations=ToolAnnotations(open_world_hint=False),
    )
    async def reschedule_booking(
        ctx: Context,
        booking_id: BookingNumber,
        starts_at: Annotated[Time | None, Field(description="新的开始时间，使用带时区的整秒日期时间。")] = None,
        ends_at: Annotated[Time | None, Field(description="新的结束时间，使用带时区的整秒日期时间。")] = None,
        room_id: Annotated[RoomNumber | None, Field(description="新的会议室编号，例如 R000001。")] = None,
        subject: Annotated[Subject | None, Field(description="新的非空会议主题。")] = None,
        content: Annotated[str | None, Field(description="新的会议内容，清空时使用 clear_content。")] = None,
        clear_content: Annotated[bool, Field(description="明确清空会议内容。不能与 content 同时提供。")] = False,
        member_ids: Annotated[list[PersonId] | None, Field(description="替换为完整的新成员名单，不含主持人；空列表清空名单。")] = None,
    ) -> Annotated[CallToolResult, BookingDetail]:
        changes = {
            "starts_at": starts_at.isoformat() if starts_at else None,
            "ends_at": ends_at.isoformat() if ends_at else None,
            "room_id": room_id,
            "subject": subject, "content": content, "member_ids": member_ids,
        }
        body = {key: value for key, value in changes.items() if value is not None}
        if clear_content:
            if content is not None:
                return api_error("VALIDATION_ERROR", "不能同时提供会议内容和清空内容")
            body["content"] = None
        return await call_api(ctx, "PATCH", f"/bookings/{booking_id}", BookingDetail, body=body)

    @server.tool(
        name="cancel_booking",
        description="按申请编号取消等待中的预约并释放原时段。仅创建人或主持人可操作，重复取消返回同一条已取消预约。",
        annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True, open_world_hint=False),
    )
    async def cancel_booking(ctx: Context, booking_id: BookingNumber) -> Annotated[CallToolResult, BookingDetail]:
        return await call_api(ctx, "POST", f"/bookings/{booking_id}/cancel", BookingDetail)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="启动独立会议室 MCP 服务")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--transport", choices=("streamable-http", "stdio"), default="streamable-http")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="TOML 配置文件路径")
    args = parser.parse_args()
    api_url = os.getenv("MEETING_API_URL", "http://127.0.0.1:8000")
    try:
        person_id = test_person_id(args.config)
        with httpx.Client(base_url=api_url, timeout=3.0, trust_env=False) as client:
            client.get("bookings", params={"limit": 1}, headers={"X-Person-ID": person_id}).raise_for_status()
        server = create_server(api_url)
    except (httpx.HTTPError, ValueError, KeyError, OSError) as error:
        raise SystemExit(f"会议室 MCP 启动失败，检查 API 地址、就绪状态或测试身份配置（{error}）") from None
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        uvicorn.run(server.streamable_http_app(stateless_http=True, json_response=True), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
