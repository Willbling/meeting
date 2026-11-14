import argparse
import asyncio
import logging
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx
from aibot import WSClient, WSClientOptions
from dotenv import dotenv_values
from pydantic import TypeAdapter, ValidationError

from agent.main import AgentError, AgentMemory, MainAgent, agent_factory
from api.main import DEFAULT_DATABASE
from api.models import PersonId
from api.store import connect

from . import memory


LOG = logging.getLogger("meeting.wecom")
PROJECT = Path(__file__).resolve().parents[1]
MEMORY_DIR = PROJECT / "data" / "wecom_memory"


def find_person(database: Path, user_id: str) -> str | None:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT person_id FROM people WHERE wecom_user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None


def bind_user(database: Path, person_id: str, user_id: str) -> None:
    person_id = TypeAdapter(PersonId).validate_python(person_id)
    if not user_id.strip():
        raise ValueError("企业微信 UserID 不能为空。")
    with closing(connect(database)) as db:
        result = db.execute("UPDATE people SET wecom_user_id = ? WHERE person_id = ?", (user_id.strip(), person_id))
        if result.rowcount == 0:
            raise ValueError(f"找不到样例人物 {person_id}。")


async def message_reply(
    body: dict, database: Path, create_agent: Callable[..., MainAgent],
    memory_dir: Path, bot_name: str = "会议室助手",
) -> str | None:
    channel = body.get("chattype")
    if channel not in {"single", "group"}:
        return None
    group = channel == "group"
    user_id = (body.get("from") or {}).get("userid")
    chat_id = body.get("chatid")
    if not user_id or (group and not chat_id):
        return "暂时无法识别这条消息的发送者或会话。"

    text = ((body.get("text") or {}).get("content") or "").strip()
    quoted = ""
    if group:
        text = text.removeprefix(f"@{bot_name}").lstrip(" \t\r\n\u2005\u200b\u00a0\u3000")
        quote = body.get("quote") or {}
        quoted = ((quote.get("text") or {}).get("content") or "").strip()
    elif text == "我的账号":
        return f"你的企业微信 UserID 是 {user_id}。"
    if not text and not quoted:
        return None

    person_id = None
    if not group:
        try:
            person_id = await asyncio.to_thread(find_person, database, user_id)
        except sqlite3.Error as error:
            LOG.warning("读取企业微信账号绑定失败（%s）", type(error).__name__)
            return "暂时无法核对你的身份，请稍后重试。"
        if person_id is None:
            return "你的企业微信账号还未绑定会议室助手。发送「我的账号」获取 UserID，请联系管理员添加绑定。"

    scope = memory_dir / "groups" / memory.file_key(chat_id) if group else memory_dir / "single"
    path = scope / f"{memory.file_key(user_id)}.txt"
    try:
        saved = await asyncio.to_thread(memory.load, path, person_id)
    except (OSError, ValidationError) as error:
        LOG.warning("无法读取企业微信会话文件（%s）", type(error).__name__)
        return "暂时无法读取这次对话的记忆，请稍后重试或联系管理员。"
    agent = create_agent(person_id, group=group)
    agent.quoted_message = quoted[:600] or None
    if saved is not None:
        agent.restore(saved)

    async def save(snapshot: AgentMemory) -> None:
        await asyncio.to_thread(memory.save, path, person_id, snapshot)

    agent.save_memory = save
    answer = await agent.reply(text)
    try:
        await agent.checkpoint()
    except OSError as error:
        LOG.warning("无法保存企业微信会话文件（%s）", type(error).__name__)
        return f"{answer}\n\n这轮对话的记忆未能保存。"
    return answer


class SdkLogger:
    # SDK debug logs can include the full frame. Log connection events ourselves.
    def debug(self, *args: object) -> None:
        pass

    info = warning = warn = error = debug


def create_bot(
    bot_id: str, secret: str, database: Path, memory_dir: Path,
    create_agent: Callable[..., MainAgent], bot_name: str = "会议室助手",
) -> WSClient:
    bot = WSClient(WSClientOptions(bot_id=bot_id, secret=secret, logger=SdkLogger()))
    conversation_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
    recent_messages: dict[str, None] = {}

    @bot.on("authenticated")
    def authenticated() -> None:
        LOG.info("企业微信已认证。请单聊或在群里 @%s 发送「你好」。", bot_name)

    @bot.on("disconnected")
    def disconnected(*args: object) -> None:
        LOG.warning("企业微信连接已断开。")

    @bot.on("error")
    def on_error(error: Exception) -> None:
        LOG.warning("企业微信连接或回复出错（%s）。请核对机器人凭据和网络。", type(error).__name__)

    @bot.on("message.text")
    async def on_text(frame: dict) -> None:
        body = frame.get("body") or {}
        msg_id = body.get("msgid")
        if msg_id:
            if msg_id in recent_messages:
                return
            recent_messages[msg_id] = None
            if len(recent_messages) > 2048:
                recent_messages.pop(next(iter(recent_messages)))
        channel = body.get("chattype") or ""
        chat_id = (body.get("chatid") or "") if channel == "group" else ""
        user_id = (body.get("from") or {}).get("userid") or ""
        key = (channel, chat_id, user_id)
        lock = conversation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            stream_id = uuid4().hex
            text = ((body.get("text") or {}).get("content") or "").strip()
            started = channel in {"single", "group"} and (text or body.get("quote")) and text != "我的账号"
            if started:
                await bot.reply_stream(frame, stream_id, "正在处理…", False)
            reply = await message_reply(body, database, create_agent, memory_dir, bot_name)
            if reply is not None or started:
                await bot.reply_stream(frame, stream_id, reply or "请发一条你想让我处理的消息。", True)

    return bot


async def run_bot(bot_id: str, secret: str, database: Path, memory_dir: Path, bot_name: str) -> None:
    async with agent_factory() as (_, create_agent):
        bot = create_bot(bot_id, secret, database, memory_dir, create_agent, bot_name)
        try:
            await bot.connect()
            await asyncio.Future()
        finally:
            bot.disconnect()
            await bot.wait_for_complete()


def main() -> None:
    parser = argparse.ArgumentParser(description="企业微信会议室助手")
    parser.add_argument("--bind", nargs=2, metavar=("PERSON_ID", "USER_ID"), help="绑定本地人员与企业微信账号")
    parser.add_argument("--memory-dir", type=Path, default=MEMORY_DIR, help="对话记忆文件夹")
    parser.add_argument("--reset-memory", action="store_true", help="启动前清空单聊和群聊记忆文件")
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
        if args.reset_memory:
            memory.reset(args.memory_dir)
            LOG.info("单聊和群聊的记忆已清空。")
        asyncio.run(run_bot(bot_id, secret, database, args.memory_dir, bot_name))
    except KeyboardInterrupt:
        pass
    except (AgentError, OSError, ValueError, httpx.InvalidURL) as error:
        parser.exit(1, f"启动企业微信助手失败：{error}\n")


if __name__ == "__main__":
    main()
