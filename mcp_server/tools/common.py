from collections.abc import Awaitable, Callable
from typing import Annotated

from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field

from api.models import ApiDatetime, BookingId, RoomId


ApiCall = Callable[..., Awaitable[CallToolResult]]
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


Limit = Annotated[int, Field(ge=1, le=100, description="每页返回条数，默认 50，最多 100。")]
Offset = Annotated[int, Field(ge=0, le=2**63 - 1, description="跳过的记录条数，默认 0。")]
Time = Annotated[ApiDatetime, Field(description="带时区的整秒日期时间，例如 2026-11-16T14:00:00+08:00")]
BookingNumber = Annotated[BookingId, Field(description="申请编号，例如 A000001，从预约查询结果取得。")]
RoomNumber = Annotated[RoomId, Field(description="会议室编号，例如 R000001，从查房结果取得。")]


class SearchResults[T](BaseModel):
    items: list[T]
