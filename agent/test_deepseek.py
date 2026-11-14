"""用项目 .env 中的 Ollama 配置测试会议室助手。"""

import argparse
import asyncio
import os
from pathlib import Path

import httpx
from dotenv import dotenv_values

from .main import AgentError, ChatModel, run_cli


async def ping(base_url: str, model_name: str) -> None:
    async with httpx.AsyncClient(base_url=base_url + "/", timeout=60, trust_env=False) as http:
        model = ChatModel(http, model_name)
        reply = await model.complete([{"role": "user", "content": "Reply with OK only."}], [])
    if not reply.content or not reply.content.strip():
        raise AgentError("Ollama 已返回响应，但模型答复为空。")
    print(f"Ollama 模型 {model_name} 回复：{reply.content.strip()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("question", nargs="?", help="测试问题，默认查找周宁的人物编号")
    mode.add_argument("--chat", action="store_true", help="进入多轮对话")
    mode.add_argument("--ping", action="store_true", help="只检查 Ollama 模型能否应答")
    args = parser.parse_args()

    config = dotenv_values(Path(__file__).resolve().parents[1] / ".env", encoding="utf-8-sig")
    base_url = (config.get("OLLAMA_API_BASE") or "http://localhost:11434").strip().rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    model = (config.get("OLLAMA_MODEL") or "deepseek-r1:1.5b").strip()
    try:
        if args.ping:
            asyncio.run(ping(base_url, model))
            return
        os.environ.update({
            "MEETING_LLM_API_KEY": "", "MEETING_LLM_BASE_URL": base_url, "MEETING_MODEL": model,
        })
        os.environ.setdefault("MEETING_PERSON_ID", "P000001")
        question = None if args.chat else (args.question or "请查一下周宁的人物编号。")
        asyncio.run(run_cli(question, trace=True, trust_env=False))
    except (AgentError, httpx.InvalidURL) as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
