import argparse
import asyncio
from pathlib import Path

import httpx
from mcp.shared.exceptions import MCPError

from service_config import DEFAULT_CONFIG, test_person_id, validate_person_id

from .conversation import MainAgent as MainAgent
from .factory import agent_factory as agent_factory
from .meeting import MeetingAgent as MeetingAgent
from .memory import AgentMemory as AgentMemory
from .model import AgentError, ChatModel as ChatModel, ModelMessage as ModelMessage


async def run_cli(
    question: str | None = None, *, trace: bool = False, model_options: dict | None = None,
    trust_env: bool = True, person_id: str | None = None, config_file: Path = DEFAULT_CONFIG,
) -> None:
    actor = test_person_id(config_file) if person_id is None else validate_person_id(person_id)
    async with agent_factory(model_options=model_options, trust_env=trust_env) as (model_name, create_agent):
        agent = create_agent(actor)
        if question is not None:
            print(f"你：{question}", flush=True)
            answer = await agent.reply(question, trace=trace)
        else:
            print(f"助手已启动，当前模型 {model_name}，测试身份 {actor}。输入 /exit 退出。")
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

    print(f"助手：{answer}")


def main() -> None:
    parser = argparse.ArgumentParser(description="会议室命令行助手")
    parser.add_argument("--person-id", help="本地测试人员编号，默认读取配置文件")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="TOML 配置文件路径")
    parser.add_argument("--trace", action="store_true", help="显示模型输入 token、意图和 MCP 调用结果")
    parser.add_argument("question", nargs="?", help="只处理一条用户消息，省略时进入多轮对话")
    args = parser.parse_args()
    try:
        asyncio.run(run_cli(args.question, trace=args.trace, person_id=args.person_id, config_file=args.config))
    except KeyboardInterrupt:
        pass
    except (AgentError, MCPError, httpx.RequestError, OSError) as error:
        raise SystemExit(f"Agent 运行失败，请检查 MCP 服务和模型配置（{error}）") from None
    except (ValueError, KeyError, httpx.InvalidURL) as error:
        raise SystemExit("Agent 配置格式有误，请检查测试人员编号和模型接口地址。") from error


if __name__ == "__main__":
    main()
