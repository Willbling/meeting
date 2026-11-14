import json
from typing import TypeVar

import httpx
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError

from api.models import ErrorResponse


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def result(payload: dict, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        is_error=is_error,
        structured_content=payload,
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
    )


def api_error(code: str, message: str) -> CallToolResult:
    return result({"code": code, "message": message}, is_error=True)


class MeetingApi:
    def __init__(self, base_url: str, actor: str) -> None:
        url = httpx.URL(base_url)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("MEETING_API_URL 必须是 HTTP 或 HTTPS 地址")
        self.base_url = url
        self.actor = actor

    async def call(
        self,
        method: str,
        path: str,
        response_model: type[ResponseModel],
        *,
        params: dict | None = None,
        body: dict | None = None,
        list_result: bool = False,
    ) -> CallToolResult:
        write = method != "GET"
        uncertain = "。写入结果未知，请先查询预约确认再决定是否重试" if write else ""
        query = {key: value for key, value in (params or {}).items() if value is not None}
        # Keep localhost requests independent of the desktop's proxy configuration.
        async with httpx.AsyncClient(base_url=self.base_url, trust_env=False, timeout=10.0) as client:
            try:
                response = await client.request(
                    method, path, params=query, json=body, headers={"X-Person-ID": self.actor},
                )
            except httpx.ConnectError:
                return api_error("API_UNAVAILABLE", "无法连接会议室 API，请检查 FastAPI 是否已启动")
            except httpx.TimeoutException:
                return api_error("API_TIMEOUT", f"会议室 API 响应超时{uncertain}")
            except httpx.RequestError:
                return api_error("API_UNAVAILABLE", f"无法完成与会议室 API 的通信{uncertain}")

        if 400 <= response.status_code < 500:
            try:
                error = ErrorResponse.model_validate_json(response.content)
            except ValidationError:
                return api_error("API_REQUEST_FAILED", f"会议室 API 拒绝了请求（HTTP {response.status_code}）")
            return result(error.model_dump(mode="json", exclude_none=True), is_error=True)
        if not response.is_success:
            return api_error("API_REQUEST_FAILED", f"会议室 API 请求失败（HTTP {response.status_code}）{uncertain}")

        try:
            data = response.json()
            payload = response_model.model_validate({"items": data} if list_result else data)
        except ValueError:
            return api_error("API_INVALID_RESPONSE", f"会议室 API 返回了无法识别的结果{uncertain}")
        return result(payload.model_dump(mode="json"))
