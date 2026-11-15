import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from aibot import WSClient, WSClientOptions

from agent.conversation import MainAgent
from agent.factory import agent_factory
from api.models import BEIJING

from .messages import message_reply


LOG = logging.getLogger("meeting.wecom")


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
        request_time = datetime.now(BEIJING)
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
            reply = await message_reply(body, database, create_agent, memory_dir, bot_name, request_time=request_time)
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
