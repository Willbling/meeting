from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult
from pydantic import Field

from api.models import AvailabilityMode, Person, Room, RoomAvailability

from .common import READ_ONLY, ApiCall, Limit, Offset, SearchResults, Time


def register_directory_tools(server: MCPServer, call_api: ApiCall) -> None:
    @server.tool(name="find_people", description="按姓名片段查人员，返回姓名、人物编号、职务和职级。同名时请让用户确认人选。", annotations=READ_ONLY)
    async def find_people(
        ctx: Context,
        name: Annotated[str, Field(min_length=1, pattern=r"\S", description="姓名或连续的姓名片段，例如周宁。")],
        limit: Limit = 50, offset: Offset = 0,
    ) -> Annotated[CallToolResult, SearchResults[Person]]:
        return await call_api(ctx, "GET", "/people", SearchResults[Person], list_result=True,
                              params={"name": name, "limit": limit, "offset": offset})

    @server.tool(name="find_available_rooms", description="查可用会议室。full 要求全程空闲；intervals 返回查询范围内的空闲和占用时段，支持最短连续空闲时长。", annotations=READ_ONLY)
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
