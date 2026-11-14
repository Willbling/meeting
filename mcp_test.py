import asyncio
import sys
from pathlib import Path
from mcp import Client, StdioServerParameters

async def main():
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=Path.cwd(),
        env={
            "MEETING_API_URL": "http://127.0.0.1:8000",
            "MEETING_PERSON_ID": "P000001",
        },
    )
    async with Client(server) as client:
        print([tool.name for tool in (await client.list_tools()).tools])
        result = await client.call_tool("find_people", {"name": "周宁"})
        print(result.structured_content)

asyncio.run(main())