import os
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field, TypeAdapter

from api.models import ApiDatetime, Booking, BookingDetail, BookingId, BookingStatus, Person, PersonId, Room, RoomId, Subject

from .client import MeetingApi


Limit = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0, le=2**63 - 1)]
Time = Annotated[ApiDatetime, Field(description="带时区的整秒日期时间，例如 2026-11-16T14:00:00+08:00")]


class SearchResults[T](BaseModel):
    items: list[T]


def create_server(api_url: str, person_id: str) -> MCPServer:
    api = MeetingApi(api_url, TypeAdapter(PersonId).validate_python(person_id))
    server = MCPServer("meeting-room-booking")
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    async def find_people(
        name: Annotated[str, Field(min_length=1, pattern=r"\S")], limit: Limit = 50, offset: Offset = 0,
    ) -> Annotated[CallToolResult, SearchResults[Person]]:
        """按姓名查人员，返回姓名和人物 ID。同名时请让用户从候选中确认。"""
        return await api.call("GET", "/people", SearchResults[Person], list_result=True,
                              params={"name": name, "limit": limit, "offset": offset})

    @server.tool(annotations=read_only)
    async def find_available_rooms(
        starts_at: Time, ends_at: Time, min_capacity: Annotated[int, Field(ge=1, le=2**63 - 1)] = 1,
        name: str | None = None, limit: Limit = 50, offset: Offset = 0,
    ) -> Annotated[CallToolResult, SearchResults[Room]]:
        """查找整个指定时段都空闲的启用会议室。所需容量应包含主持人。"""
        return await api.call("GET", "/rooms", SearchResults[Room], list_result=True, params={
            "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat(), "min_capacity": min_capacity,
            "name": name, "limit": limit, "offset": offset,
        })

    @server.tool(annotations=read_only)
    async def list_my_bookings(
        room_id: RoomId | None = None, status: BookingStatus | None = None,
        starts_at: Time | None = None, ends_at: Time | None = None,
        limit: Limit = 50, offset: Offset = 0,
    ) -> Annotated[CallToolResult, SearchResults[Booking]]:
        """查当前操作者创建、主持或参加的预约，可按状态、房间和相交时段筛选。"""
        return await api.call("GET", "/bookings", SearchResults[Booking], list_result=True, params={
            "room_id": room_id, "status": status, "starts_at": starts_at.isoformat() if starts_at else None,
            "ends_at": ends_at.isoformat() if ends_at else None, "limit": limit, "offset": offset,
        })

    @server.tool(annotations=read_only)
    async def get_booking(booking_id: BookingId) -> Annotated[CallToolResult, BookingDetail]:
        """按申请 ID 查预约详情，包括北京时间、状态、主持人和成员。"""
        return await api.call("GET", f"/bookings/{booking_id}", BookingDetail)

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False, open_world_hint=False))
    async def create_booking(
        room_id: RoomId, host_id: PersonId, starts_at: Time, ends_at: Time, subject: Subject,
        member_ids: list[PersonId] | None = None, content: str | None = None,
    ) -> Annotated[CallToolResult, BookingDetail]:
        """创建未来的会议预约。host_id 可指定同事以代订，member_ids 填其他参会人。成功后返回申请 ID。"""
        return await api.call("POST", "/bookings", BookingDetail, body={
            "room_id": room_id, "host_id": host_id, "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(), "subject": subject, "member_ids": member_ids or [], "content": content,
        })

    @server.tool(annotations=ToolAnnotations(open_world_hint=False))
    async def reschedule_booking(
        booking_id: BookingId, starts_at: Time | None = None, ends_at: Time | None = None,
        room_id: RoomId | None = None,
    ) -> Annotated[CallToolResult, BookingDetail]:
        """按明确的申请 ID 改期或换会议室，至少填写一项。仅创建人或主持人可改等待中的预约。"""
        changes = {
            "starts_at": starts_at.isoformat() if starts_at else None,
            "ends_at": ends_at.isoformat() if ends_at else None,
            "room_id": room_id,
        }
        return await api.call("PATCH", f"/bookings/{booking_id}", BookingDetail,
                              body={key: value for key, value in changes.items() if value is not None})

    @server.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    async def cancel_booking(booking_id: BookingId) -> Annotated[CallToolResult, BookingDetail]:
        """按明确的申请 ID 取消预约并释放时段。仅创建人或主持人可取消等待中的预约。"""
        return await api.call("POST", f"/bookings/{booking_id}/cancel", BookingDetail)

    return server


def main() -> None:
    person_id = os.getenv("MEETING_PERSON_ID")
    if not person_id:
        raise SystemExit("请设置 MEETING_PERSON_ID，例如 P000001（本地联调身份）")
    api_url = os.getenv("MEETING_API_URL", "http://127.0.0.1:8000")
    create_server(api_url, person_id).run(transport="stdio")


if __name__ == "__main__":
    main()
