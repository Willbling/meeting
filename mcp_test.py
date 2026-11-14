"""通过 HTTP 检查独立 MCP 服务及其到 API 的连接。"""

import argparse
import asyncio
import os
from pathlib import Path

from mcp import Client
from rich import print as rprint

from mcp_server.client import ACTOR_META_KEY
from service_config import DEFAULT_CONFIG, test_person_id


async def check(person_id: str) -> None:
    url = os.getenv("MEETING_MCP_URL", "http://127.0.0.1:8001/mcp")
    async with Client(url, read_timeout_seconds=5) as client:
        for tool in (await client.list_tools()).tools:
            rprint(tool)
        result = await client.call_tool("list_my_bookings", {"limit": 1}, meta={ACTOR_META_KEY: person_id})
        if result.is_error:
            raise SystemExit(result.structured_content or result.content)
        print(f"MCP 可使用测试身份 {person_id} 查询 API。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    asyncio.run(check(test_person_id(parser.parse_args().config)))
