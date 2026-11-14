import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from jsonschema import Draft202012Validator
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

from api.models import BEIJING


DIRECTORY = Path(__file__).resolve().parent
WRITE_TOOLS = {"create_booking", "reschedule_booking", "cancel_booking"}
MAX_TOOL_CALLS = 8


class AgentError(Exception):
    pass


class FunctionCall(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: Literal["function"]
    function: FunctionCall


class ModelMessage(BaseModel):
    role: Literal["assistant"]
    content: str | None = None
    refusal: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] | None = None


class ChatModel:
    def __init__(self, http: httpx.AsyncClient, model: str, *, options: dict | None = None) -> None:
        self.http = http
        self.model = model
        self.options = options or {}

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelMessage:
        tool_options = {"tools": tools, "tool_choice": "auto"} if tools else {}
        try:
            response = await self.http.post("chat/completions", json={
                **self.options,
                "model": self.model,
                "messages": messages,
                **tool_options,
            })
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise AgentError(f"模型接口返回 HTTP {error.response.status_code}，请检查模型、地址和访问权限。") from error
        except httpx.RequestError as error:
            raise AgentError("无法连接模型接口或请求超时。") from error

        try:
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise AgentError("模型输出被截断或拦截，请缩小请求范围后再试。")
            return ModelMessage.model_validate(choice["message"])
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            raise AgentError("模型响应格式有误，请使用支持函数调用的 Chat Completions 模型。") from error


class MeetingAgent:
    def __init__(self, client: Client, model: ChatModel, person_id: str) -> None:
        self.client = client
        self.model = model
        self.person_id = person_id
        self.prompt = (DIRECTORY / "prompts.md").read_text(encoding="utf-8")
        self.history: list[dict] = []
        self.tools: list[dict] = []
        self.validators: dict[str, Draft202012Validator] = {}

    async def discover_tools(self) -> None:
        available = (await self.client.list_tools()).tools
        self.tools.clear()
        self.validators.clear()
        for tool in available:
            schema = {**tool.input_schema, "additionalProperties": False}
            self.tools.append({"type": "function", "function": {
                "name": tool.name, "description": tool.description or "", "parameters": schema,
            }})
            self.validators[tool.name] = Draft202012Validator(schema)

    async def execute(self, function: FunctionCall) -> dict:
        validator = self.validators.get(function.name)
        if validator is None:
            return {"is_error": True, "code": "UNKNOWN_TOOL", "message": "请选择已提供的工具。"}
        try:
            arguments = json.loads(function.arguments)
        except ValueError:
            return {"is_error": True, "code": "INVALID_ARGUMENTS", "message": "工具参数需要是 JSON 对象。"}
        issue = next(validator.iter_errors(arguments), None)
        if issue is not None:
            return {"is_error": True, "code": "INVALID_ARGUMENTS", "message": issue.message}

        try:
            result = await self.client.call_tool(function.name, arguments)
        except (MCPError, TimeoutError, OSError):
            return {"is_error": True, "code": "MCP_CALL_FAILED", "message": "工具调用中断，请先查询以确认写入结果。"}
        data = result.structured_content
        if data is None:
            data = {"message": "\n".join(block.text for block in result.content if block.type == "text")}
        return {**data, "is_error": result.is_error}

    async def reply(self, text: str, *, trace: bool = False) -> str:
        now = datetime.now(BEIJING).isoformat(timespec="seconds")
        system = {"role": "system", "content": f"{self.prompt}\n当前北京时间 {now}。当前操作者 {self.person_id}。"}
        self.history.append({"role": "user", "content": text})
        remaining = MAX_TOOL_CALLS
        attempted_write = False
        check_write_result = False
        while True:
            try:
                message = await self.model.complete([system, *self.history], self.tools)
            except AgentError as error:
                answer = str(error)
                if attempted_write:
                    answer += "本轮已尝试写入，请先查询预约结果再决定是否重试。"
                break

            calls = message.tool_calls or []
            if not calls:
                answer = message.content or message.refusal or "模型没有返回可显示的答复。"
                if attempted_write and not message.content and not message.refusal:
                    answer += "请先查询预约以确认本轮的写入结果。"
                break
            if len(calls) > remaining or len({call.id for call in calls}) != len(calls):
                answer = "本轮工具调用超出上限或调用 ID 重复，已停止。请先查询本轮预约结果。"
                break

            self.history.append(message.model_dump(exclude_none=True))
            remaining -= len(calls)
            for call in calls:
                if trace:
                    print(f"[工具] {call.function.name} {call.function.arguments}", flush=True)
                write = call.function.name in WRITE_TOOLS
                if write and check_write_result:
                    output = {
                        "is_error": True, "code": "WRITE_RESULT_UNKNOWN",
                        "message": "本轮已有结果未知的写入。请先查询并告知用户结果，再由用户决定后续操作。",
                    }
                else:
                    attempted_write = attempted_write or write
                    output = await self.execute(call.function)
                    if write and output.get("code") in {
                        "API_TIMEOUT", "API_UNAVAILABLE", "API_INVALID_RESPONSE", "API_REQUEST_FAILED", "MCP_CALL_FAILED",
                    }:
                        check_write_result = True
                if trace:
                    status = output.get("code", "失败") if output["is_error"] else "成功"
                    print(f"[结果] {status}", flush=True)
                self.history.append({
                    "role": "tool", "tool_call_id": call.id, "content": json.dumps(output, ensure_ascii=False),
                })

        self.history.append({"role": "assistant", "content": answer})
        return answer


async def run_cli(
    question: str | None = None, *, trace: bool = False, model_options: dict | None = None,
    trust_env: bool = True,
) -> None:
    model_name = os.getenv("MEETING_MODEL", "").strip()
    person_id = os.getenv("MEETING_PERSON_ID", "").strip()
    if not model_name or not person_id:
        raise SystemExit("请设置 MEETING_MODEL 和 MEETING_PERSON_ID（本地联调可用 P000001）。")
    base_url = os.getenv("MEETING_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/"
    url = httpx.URL(base_url)
    if url.scheme not in {"http", "https"} or not url.host:
        raise SystemExit("MEETING_LLM_BASE_URL 必须是模型接口的 HTTP 或 HTTPS 基础地址。")
    api_key = os.getenv("MEETING_LLM_API_KEY", "").strip()
    if url.host == "api.openai.com" and not api_key:
        raise SystemExit("请设置 MEETING_LLM_API_KEY，供模型接口认证。")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    server = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "mcp_server.server"],
        cwd=DIRECTORY.parent,
        env={
            "MEETING_API_URL": os.getenv("MEETING_API_URL", "http://127.0.0.1:8000"),
            "MEETING_PERSON_ID": person_id,
        },
    )
    async with (
        Client(server) as client,
        httpx.AsyncClient(base_url=url, headers=headers, timeout=60, trust_env=trust_env) as http,
    ):
        agent = MeetingAgent(client, ChatModel(http, model_name, options=model_options), person_id)
        await agent.discover_tools()
        if question is not None:
            print(f"你：{question}", flush=True)
            answer = await agent.reply(question, trace=trace)
            tool_results = [json.loads(message["content"]) for message in agent.history if message["role"] == "tool"]
        else:
            print(f"会议室助手已连接 {len(agent.tools)} 个工具。输入 /exit 退出。")
            while True:
                try:
                    text = input("你：").strip()
                except EOFError:
                    break
                if text == "/exit":
                    break
                if text:
                    print(f"助手：{await agent.reply(text, trace=trace)}")
            return

    if not any(result["is_error"] is False for result in tool_results):
        print(f"模型原始回复：{answer}", flush=True)
        raise AgentError("没有成功执行会议工具。请查看上方的工具轨迹和模型回复。")
    print(f"助手：{answer}")


def main() -> None:
    try:
        asyncio.run(run_cli())
    except KeyboardInterrupt:
        pass
    except (ValidationError, httpx.InvalidURL) as error:
        raise SystemExit("模型接口或 MCP 启动参数格式有误。") from error


if __name__ == "__main__":
    main()
