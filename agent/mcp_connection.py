import asyncio

import httpx2
from mcp import Client
from mcp.shared.exceptions import MCPError

from .model import AgentError


MCP_CONNECTION_ERRORS = (MCPError, httpx2.HTTPError, OSError, TimeoutError)


async def serve_mcp(url: str, connected: asyncio.Future[Client], close: asyncio.Event) -> None:
    # The MCP SDK's transport must be opened and closed in the same task.
    try:
        try:
            async with Client(url, read_timeout_seconds=15) as client:
                connected.set_result(client)
                await close.wait()
        except* MCP_CONNECTION_ERRORS as error:
            raise AgentError("会议服务暂时连不上，请检查 API 和 MCP 是否已启动。") from error
    except Exception as error:
        if connected.done():
            raise
        connected.set_exception(error)
        connected.exception()
