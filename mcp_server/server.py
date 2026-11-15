import argparse
import os
from pathlib import Path

import httpx
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult
from pydantic import BaseModel, ValidationError

from service_config import DEFAULT_CONFIG, test_person_id, validate_person_id

from .client import ACTOR_META_KEY, MeetingApi, api_error
from .tools.bookings import register_booking_tools
from .tools.directory import register_directory_tools


def create_server(api_url: str) -> MCPServer:
    server = MCPServer("meeting-room-booking", instructions="会议室预订工具。调用方随每次工具请求提供当前操作者。")

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

    register_directory_tools(server, call_api)
    register_booking_tools(server, call_api)
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
