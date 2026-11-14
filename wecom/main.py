import argparse
import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx
from aibot import WSClient, WSClientOptions
from dotenv import dotenv_values
from pydantic import TypeAdapter, ValidationError

from api.main import DEFAULT_DATABASE
from api.models import BEIJING, Booking, PersonId, Room
from api.store import connect


LOG = logging.getLogger("meeting.wecom")
PROJECT = Path(__file__).resolve().parents[1]
BOOKING_LIST = TypeAdapter(list[Booking])
ROOM_LIST = TypeAdapter(list[Room])
HELP = "连接成功。发送「我的账号」查看企业微信 UserID，发送「查我的预约」查询预约。"
PAGE_SIZE = 5
GROUP_HELP = "群里可以查空闲会议室。请发送「查空闲会议室 明天 14:00-15:00 8人」。人数可省略。"
ROOM_COMMAND = "查空闲会议室"
ROOM_FIELDS = re.compile(
    r"(?:(今天|明天|[0-9]{4}-[0-9]{2}-[0-9]{2})\s*)?"
    r"(?:([0-9]{2}:[0-9]{2})\s*[-–—至]\s*([0-9]{2}:[0-9]{2})\s*)?"
    r"(?:([1-9][0-9]{0,8})\s*人)?"
)
GROUP_DRAFT_TTL = 300
type RoomFields = tuple[str | None, str | None, str | None, str | None]
type GroupDrafts = dict[tuple[str, str], tuple[float, RoomFields]]


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


async def group_reply(
    body: dict, text: str, http: httpx.AsyncClient, bot_name: str, drafts: GroupDrafts,
) -> str:
    # WeCom forwards group mentions. The text may still include the display name.
    text = text.removeprefix(f"@{bot_name}").lstrip(" \t\r\n\u2005\u200b\u00a0\u3000")
    if text in {"我的账号", "查我的预约"}:
        return "请单聊「会议室助手」发送这条指令。"
    if text == "你好":
        return f"连接成功。{GROUP_HELP}"

    if text in {"", "查一下"}:
        quote = body.get("quote") or {}
        quoted_text = ((quote.get("text") or {}).get("content") or "").strip()
        if quote.get("msgtype") != "text" or not quoted_text.startswith(ROOM_COMMAND):
            return "请在同一条消息里 @我 并附上查询，或引用原查询后 @我。"
        text = quoted_text

    chat_id = body.get("chatid")
    user_id = (body.get("from") or {}).get("userid")
    key = (chat_id, user_id) if chat_id and user_id else None
    now = monotonic()
    for draft_key, (expires, _) in list(drafts.items()):
        if expires <= now:
            del drafts[draft_key]
    previous = drafts.get(key) if key is not None else None
    new_query = text.startswith(ROOM_COMMAND)
    if not new_query and previous is None:
        return GROUP_HELP
    match = ROOM_FIELDS.fullmatch(text.removeprefix(ROOM_COMMAND).strip())
    if match is None:
        return GROUP_HELP

    day, start, end, capacity = match.groups()
    if previous is not None and not new_query:
        _, (old_day, old_start, old_end, old_capacity) = previous
        day, start, end, capacity = day or old_day, start or old_start, end or old_end, capacity or old_capacity
    if day in {"今天", "明天"}:
        day = str(datetime.now(BEIJING).date() + timedelta(days=int(day == "明天")))

    try:
        on_date = date.fromisoformat(day) if day else None
        start_time = time.fromisoformat(start) if start else None
        end_time = time.fromisoformat(end) if end else None
        if start_time is not None and end_time is not None and start_time >= end_time:
            return "结束时间必须晚于开始时间，请填写同一天内的时段。"
    except ValueError:
        return "日期或时间有误，请参考「查空闲会议室 明天 14:00-15:00 8人」。"

    if on_date is None or start_time is None or end_time is None:
        if key is None:
            return GROUP_HELP
        drafts[key] = (now + GROUP_DRAFT_TTL, (day, start, end, capacity))
        missing = "日期和时段，例如「明天 14:00-15:00」"
        if on_date is not None:
            missing = "时段，例如「14:00-15:00 8人」"
        elif start_time is not None:
            missing = "日期，例如「明天」"
        return f"请在本群再次 @我，补充{missing}。"

    if key is not None:
        drafts.pop(key, None)
    starts_at = datetime.combine(on_date, start_time, BEIJING)
    ends_at = datetime.combine(on_date, end_time, BEIJING)
    try:
        async with asyncio.timeout(3):
            response = await http.get("rooms", params={
                "starts_at": starts_at.isoformat(timespec="seconds"),
                "ends_at": ends_at.isoformat(timespec="seconds"),
                "min_capacity": int(capacity or 1),
                "limit": PAGE_SIZE + 1,
            })
            response.raise_for_status()
            rooms = ROOM_LIST.validate_json(response.content)
    except (TimeoutError, httpx.HTTPError, ValidationError) as error:
        LOG.warning("查询空闲会议室失败（%s）", type(error).__name__)
        return "暂时无法查询会议室，请检查会议室 API 是否已启动。"

    window = f"{on_date:%Y-%m-%d} {start}–{end}（北京时间）"
    if not rooms:
        return f"{window}没有符合条件的空闲会议室。"
    lines = [f"{window}的空闲会议室"]
    for room in rooms[:PAGE_SIZE]:
        lines.append(f"- {room.name}（{room.room_id}，{room.capacity} 人）｜{room.address}")
    if len(rooms) > PAGE_SIZE:
        lines.append(f"以上显示前 {PAGE_SIZE} 间。")
    return "\n".join(lines)


async def message_reply(
    body: dict, database: Path, http: httpx.AsyncClient, bot_name: str = "会议室助手",
    drafts: GroupDrafts | None = None,
) -> str | None:
    text = (body.get("text") or {}).get("content", "").strip()
    if body.get("chattype") == "group":
        return await group_reply(body, text, http, bot_name, drafts if drafts is not None else {})
    if body.get("chattype") != "single":
        return None
    user_id = (body.get("from") or {}).get("userid")
    if not user_id:
        return "未收到发件人的企业微信 UserID。"
    if text == "我的账号":
        return f"你的企业微信 UserID 是 {user_id}。"
    if text != "查我的预约":
        return HELP

    try:
        async with asyncio.timeout(3):
            person_id = await asyncio.to_thread(find_person, database, user_id)
            if person_id is None:
                return "你的企业微信账号还未绑定。发送「我的账号」获取 UserID，请管理员绑定。"
            response = await http.get(
                "bookings", params={"limit": PAGE_SIZE + 1}, headers={"X-Person-ID": person_id},
            )
            response.raise_for_status()
            bookings = BOOKING_LIST.validate_json(response.content)
    except (TimeoutError, sqlite3.Error, httpx.HTTPError, ValidationError) as error:
        LOG.warning("查询预约失败（%s）", type(error).__name__)
        return "暂时无法查询预约，请检查会议室 API 是否已启动。"

    if not bookings:
        return "没有查到你的预约。"
    lines = ["你创建、主持或参加的预约"]
    for booking in bookings[:PAGE_SIZE]:
        subject = " ".join(booking.subject.split())[:72]
        lines.append(
            f"- {booking.booking_id} {subject}｜{booking.status}｜{booking.room_id}\n"
            f"  {booking.starts_at:%m-%d %H:%M} 至 {booking.ends_at:%m-%d %H:%M}（北京时间）"
        )
    if len(bookings) > PAGE_SIZE:
        lines.append(f"以上为按开始时间排列的前 {PAGE_SIZE} 条。")
    return "\n".join(lines)


class SdkLogger:
    # SDK debug logs can include the full frame. Log connection events ourselves.
    def debug(self, *args: object) -> None:
        pass

    info = warning = warn = error = debug


def create_bot(bot_id: str, secret: str, database: Path, api_url: str, bot_name: str = "会议室助手") -> WSClient:
    bot = WSClient(WSClientOptions(bot_id=bot_id, secret=secret, logger=SdkLogger()))
    drafts: GroupDrafts = {}

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
        async with httpx.AsyncClient(base_url=api_url.rstrip("/") + "/", trust_env=False, timeout=2) as http:
            reply = await message_reply(frame.get("body") or {}, database, http, bot_name, drafts)
        if reply is not None:
            await bot.reply_stream(frame, uuid4().hex, reply, True)

    return bot


def main() -> None:
    parser = argparse.ArgumentParser(description="企业微信会议室助手")
    parser.add_argument("--bind", nargs=2, metavar=("PERSON_ID", "USER_ID"), help="绑定本地人员与企业微信账号")
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
    api_url = os.getenv("MEETING_API_URL", "http://127.0.0.1:8000")
    create_bot(bot_id, secret, database, api_url, bot_name).run()


if __name__ == "__main__":
    main()
