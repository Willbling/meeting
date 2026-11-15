import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import dotenv_values
from mcp import Client

from service_config import validate_person_id

from .conversation import MainAgent
from .mcp_connection import MCP_CONNECTION_ERRORS, serve_mcp
from .meeting import MeetingAgent
from .model import AgentError, ChatModel


PROJECT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def agent_factory(
    *, model_options: dict | None = None,
    trust_env: bool = True,
) -> AsyncIterator[tuple[str, Callable[..., MainAgent]]]:
    config = dotenv_values(PROJECT / ".env", encoding="utf-8-sig")
    model_name = (os.getenv("MEETING_MODEL") or config.get("MEETING_MODEL") or "qwen3.5:4b").strip()
    ollama_url = str(os.getenv("OLLAMA_API_BASE") or config.get("OLLAMA_API_BASE") or "http://localhost:11434").rstrip("/")
    if not ollama_url.endswith("/v1"):
        ollama_url += "/v1"
    base_url = os.getenv("MEETING_LLM_BASE_URL", ollama_url).rstrip("/") + "/"
    url = httpx.URL(base_url)
    if url.scheme not in {"http", "https"} or not url.host:
        raise AgentError("MEETING_LLM_BASE_URL 必须是模型接口的 HTTP 或 HTTPS 基础地址。")
    api_key = os.getenv("MEETING_LLM_API_KEY", "").strip()
    if url.host == "api.openai.com" and not api_key:
        raise AgentError("请设置 MEETING_LLM_API_KEY，供模型接口认证。")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    mcp_url = os.getenv("MEETING_MCP_URL", "http://127.0.0.1:8001/mcp")
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=60, trust_env=trust_env) as http:
        options = {"temperature": 0, **(model_options or {})}
        if model_name.startswith("qwen3.5"):
            options.setdefault("reasoning_effort", "none")
        model = ChatModel(http, model_name, options=options)

        ready: asyncio.Future[Client] | None = None
        connection: asyncio.Task[None] | None = None
        close = asyncio.Event()

        def create_agent(person_id: str | None, *, group: bool = False) -> MainAgent:
            actor = None if group else validate_person_id(person_id)

            async def connect_meeting() -> MeetingAgent:
                nonlocal ready, connection
                if connection is None or connection.done():
                    ready = asyncio.get_running_loop().create_future()
                    connection = asyncio.create_task(serve_mcp(mcp_url, ready, close))
                assert ready is not None
                meeting = MeetingAgent(await asyncio.shield(ready), model, actor, group=group)
                try:
                    await meeting.discover_tools()
                except MCP_CONNECTION_ERRORS as error:
                    raise AgentError("会议工具暂时无法读取，请检查 MCP。") from error
                return meeting

            agent = MainAgent(model, connect_meeting)
            agent.group = group
            return agent

        try:
            yield model_name, create_agent
        finally:
            if connection is not None:
                close.set()
                if ready is not None and not ready.done():
                    connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)
