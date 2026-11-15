import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import TypeAdapter

from api.models import PersonId
from api.store import connect


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
