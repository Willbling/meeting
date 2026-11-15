from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from api.models import Booking, BookingDetail, BookingStatus, PersonId, Subject

from ..client import api_error
from .common import READ_ONLY, ApiCall, BookingNumber, Limit, Offset, RoomNumber, SearchResults, Time


def register_booking_tools(server: MCPServer, call_api: ApiCall) -> None:
    @server.tool(name="list_my_bookings", description="查当前操作者创建、主持或参加的预约。时段筛选匹配相交的预约，结果包含会议室名、地址、主持人和预订人。", annotations=READ_ONLY)
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

    @server.tool(name="get_booking", description="按申请编号查预约详情，包括北京时间、房间名称和地址、会议内容、预订人、主持人与成员。只有创建人、主持人或成员可查看。", annotations=READ_ONLY)
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
