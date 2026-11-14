import json
from typing import TypeVar

import httpx
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError

from api.models import ErrorResponse


ACTOR_META_KEY = "meeting/person-id"
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def result(payload: dict, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        is_error=is_error,
        structured_content=payload,
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
    )


def api_error(code: str, message: str, *, http_status: int | None = None) -> CallToolResult:
    payload = {"code": code, "message": message}
    if http_status is not None:
        payload["http_status"] = http_status
    return result(payload, is_error=True)


class MeetingApi:
    def __init__(self, base_url: str, actor: str | None) -> None:
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
                    method, path, params=query, json=body, headers={"X-Person-ID": self.actor} if self.actor else None,
                )
            except httpx.ConnectError:
                return api_error("API_UNAVAILABLE", "无法连接会议室 API，请检查 FastAPI 是否已启动")
            except httpx.TimeoutException:
                return api_error("API_TIMEOUT", f"会议室 API 响应超时{uncertain}")
            except httpx.RequestError:
                return api_error("API_UNAVAILABLE", f"无法完成与会议室 API 的通信{uncertain}")

        if not response.is_success:
            status = response.status_code
            try:
                error = ErrorResponse.model_validate_json(response.content)
            except ValidationError:
                error = None
            if error and (400 <= status < 500 or error.code in {"DATABASE_BUSY", "DATABASE_READONLY"}):
                return result({**error.model_dump(mode="json", exclude_none=True), "http_status": status}, is_error=True)
            if 400 <= status < 500:
                return api_error("API_REQUEST_REJECTED", f"会议室 API 拒绝了请求（HTTP {status}）", http_status=status)
            return api_error("API_REQUEST_FAILED", f"会议室 API 请求失败（HTTP {status}）{uncertain}", http_status=status)

        try:
            data = response.json()
            payload = response_model.model_validate({"items": data} if list_result else data)
        except ValueError:
            return api_error("API_INVALID_RESPONSE", f"会议室 API 返回了无法识别的结果{uncertain}")
        return result(payload.model_dump(mode="json"))
