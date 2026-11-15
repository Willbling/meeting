"""从配置文件启动本机 FastAPI 和 MCP 服务。"""

import argparse
import asyncio
import os
import socket
import sqlite3
import subprocess
import sys
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic

import httpx
from mcp import Client
from mcp.shared.exceptions import MCPError

from data.sqlite.init_db import initialize
from service_config import DEFAULT_CONFIG


ROOT = Path(__file__).resolve().parent


async def wait_until_ready(
    process: subprocess.Popen, name: str, check: Callable[[], Awaitable[None]], timeout: int,
    *, probe_timeout: int = 2,
) -> None:
    deadline = monotonic() + timeout
    while process.poll() is None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError(f"{name} 在 {timeout} 秒内未就绪，请查看服务输出。")
        try:
            await asyncio.wait_for(check(), timeout=min(probe_timeout, remaining))
        except (httpx.HTTPError, MCPError, RuntimeError, TimeoutError, OSError, ValueError):
            await asyncio.sleep(0.2)
        else:
            if process.poll() is None:
                return
    raise RuntimeError(f"{name} 提前退出，退出码为 {process.returncode}。")


async def run(config_file: Path) -> None:
    config = tomllib.loads(config_file.read_text(encoding="utf-8"))
    api_port, mcp_port = config["api"]["port"], config["mcp"]["port"]
    timeout = config["startup"]["timeout_seconds"]
    if any(type(port) is not int or not 1 <= port <= 65535 for port in (api_port, mcp_port)):
        raise ValueError("端口必须是 1 到 65535 之间的整数。")
    if api_port == mcp_port:
        raise ValueError("API 和 MCP 必须使用不同端口。")
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("timeout_seconds 必须是正整数。")
    for port in (api_port, mcp_port):
        with socket.socket() as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise ValueError(f"本机端口 {port} 无法使用：{error}") from error
    database = (config_file.parent / config["api"]["database"]).resolve()
    if not database.is_file():
        initialize(database)
    api_url = f"http://127.0.0.1:{api_port}"
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    env = {
        **os.environ,
        "MEETING_DB_PATH": str(database),
        "MEETING_API_URL": api_url,
    }
    processes: list[subprocess.Popen] = []

    def launch(*args: str) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", *args], cwd=ROOT, env=env)
        processes.append(process)
        return process

    async with httpx.AsyncClient(trust_env=False) as http:
        async def check_api() -> None:
            response = await http.get(f"{api_url}/readyz")
            response.raise_for_status()
            if response.json() != {"status": "ready"}:
                raise RuntimeError("API 响应格式不匹配。")

        async def check_mcp() -> None:
            async with Client(mcp_url, read_timeout_seconds=4) as client:
                if not (await client.list_tools()).tools:
                    raise RuntimeError("MCP 未提供工具。")

        try:
            api = launch("uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(api_port))
            await wait_until_ready(api, "FastAPI", check_api, timeout)
            mcp = launch("mcp_server.server", "--port", str(mcp_port), "--config", str(config_file))
            await wait_until_ready(mcp, "MCP", check_mcp, timeout, probe_timeout=5)
            print(f"服务已就绪\nFastAPI: {api_url}/docs\nMCP: {mcp_url}\n数据库: {database}\n按 Ctrl+C 停止。", flush=True)
            while api.poll() is None and mcp.poll() is None:
                await asyncio.sleep(0.3)
            raise RuntimeError(f"服务进程退出（FastAPI={api.poll()}，MCP={mcp.poll()}）。")
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="TOML 配置文件路径")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.config.resolve()))
    except KeyboardInterrupt:
        pass
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"启动失败：{error}\n")


if __name__ == "__main__":
    main()
