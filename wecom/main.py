import argparse
import asyncio
import logging
import os
import sqlite3
from pathlib import Path

import httpx
from dotenv import dotenv_values

from agent.model import AgentError
from data.sqlite.init_db import DEFAULT_DATABASE

from . import memory
from .bot import create_bot as create_bot, run_bot
from .identity import bind_user, find_person as find_person
from .messages import message_reply as message_reply


LOG = logging.getLogger("meeting.wecom")
PROJECT = Path(__file__).resolve().parents[1]
MEMORY_DIR = PROJECT / "data" / "wecom_memory"


def main() -> None:
    parser = argparse.ArgumentParser(description="企业微信会议室助手")
    parser.add_argument("--bind", nargs=2, metavar=("PERSON_ID", "USER_ID"), help="绑定本地人员与企业微信账号")
    parser.add_argument("--memory-dir", type=Path, default=MEMORY_DIR, help="对话记忆文件夹")
    parser.add_argument(
        "--reset-memory", type=str.casefold, choices=("true", "false"), default="false",
        help="false 清空对话记忆后启动（默认），true 保留对话记忆",
    )
    args = parser.parse_args()
    database = Path(os.getenv("MEETING_DB_PATH") or DEFAULT_DATABASE)
    if args.bind:
        try:
            bind_user(database, *args.bind)
        except sqlite3.IntegrityError:
            parser.error("该企业微信 UserID 已绑定其他人员。")
        except (sqlite3.Error, ValueError) as error:
            parser.error(str(error))
        print("账号绑定成功。")
        return

    cfg = dotenv_values(PROJECT / ".env", encoding="utf-8-sig")
    bot_id = (os.getenv("WECOM_BOT_ID") or cfg.get("WECOM_BOT_ID") or "").strip()
    secret = (os.getenv("WECOM_BOT_SECRET") or cfg.get("WECOM_BOT_SECRET") or "").strip()
    if not bot_id or not secret:
        parser.error("请在项目根目录 .env 中配置 WECOM_BOT_ID 和 WECOM_BOT_SECRET。")
    bot_name = (os.getenv("WECOM_BOT_NAME") or cfg.get("WECOM_BOT_NAME") or "会议室助手").strip()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    LOG.info("正在连接企业微信智能机器人…")
    try:
        args.memory_dir.mkdir(parents=True, exist_ok=True)
        if args.reset_memory == "false":
            memory.reset(args.memory_dir)
            LOG.info("单聊和群聊的记忆已清空。")
        asyncio.run(run_bot(bot_id, secret, database, args.memory_dir, bot_name))
    except KeyboardInterrupt:
        pass
    except (AgentError, OSError, ValueError, httpx.InvalidURL) as error:
        parser.exit(1, f"启动企业微信助手失败：{error}\n")


if __name__ == "__main__":
    main()
