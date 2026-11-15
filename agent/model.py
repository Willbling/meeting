import re
from typing import Literal

import httpx
from pydantic import BaseModel

from .workflow_models import Interpretation


class AgentError(Exception):
    pass


class ModelMessage(BaseModel):
    role: Literal["assistant"]
    content: str | None = None

    def visible_content(self) -> str:
        content = re.sub(r"<think\b[^>]*>.*?(?:</think\s*>|\Z)", "", self.content or "", flags=re.I | re.S)
        # Some compatible endpoints echo a closing tag without the opening tag.
        return re.split(r"</think\s*>", content, flags=re.I)[-1].strip()


class ChatModel:
    def __init__(self, http: httpx.AsyncClient, model: str, *, options: dict | None = None) -> None:
        self.http = http
        self.model = model
        self.options = options or {}

    async def complete(
        self, messages: list[dict], *, schema: type[BaseModel] | None = None, trace: bool = False,
    ) -> ModelMessage:
        payload = {**self.options, "model": self.model, "messages": messages}
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            }
        try:
            response = await self.http.post("chat/completions", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise AgentError(f"模型接口返回 HTTP {error.response.status_code}，请检查模型、地址和访问权限。") from error
        except httpx.RequestError as error:
            raise AgentError("无法连接模型接口或请求超时。") from error

        try:
            data = response.json()
            if trace:
                tokens = (data.get("usage") or {}).get("prompt_tokens", "未知")
                print(f"[模型] {self.model}，输入 token {tokens}，tools 0", flush=True)
            choice = data["choices"][0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise AgentError("模型输出被截断或拦截，请缩小请求范围后再试。")
            return ModelMessage.model_validate(choice["message"])
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            raise AgentError("模型响应格式有误，请检查结构化输出配置。") from error

    async def interpret(self, messages: list[dict], *, trace: bool = False) -> Interpretation:
        message = await self.complete(messages, schema=Interpretation, trace=trace)
        if not (content := message.visible_content()):
            raise AgentError("模型没有返回结构化意图。")
        return Interpretation.model_validate_json(content)
