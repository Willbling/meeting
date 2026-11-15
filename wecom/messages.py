import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from agent.conversation import MainAgent
from agent.memory import AgentMemory

from . import memory
from .identity import find_person


LOG = logging.getLogger("meeting.wecom")


async def message_reply(
    body: dict, database: Path, create_agent: Callable[..., MainAgent],
    memory_dir: Path, bot_name: str = "会议室助手", *, request_time: datetime | None = None,
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
    answer = await agent.reply(text, request_time=request_time)
    try:
        await agent.checkpoint()
    except OSError as error:
        LOG.warning("无法保存企业微信会话文件（%s）", type(error).__name__)
        return f"{answer}\n\n这轮对话的记忆未能保存。"
    return answer
