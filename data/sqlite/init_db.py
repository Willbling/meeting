import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent
DEFAULT_DATABASE = DIRECTORY / "meeting.db"
SCHEMA_VERSION = 2


def initialize(database: Path) -> bool:
    """Create the schema and sample data once. Return whether they were created."""
    with closing(sqlite3.connect(database, autocommit=True)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            connection.execute("COMMIT")
            return False
        if version != 0 or connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
            raise ValueError("目标数据库已有其他结构，请用 --database 指定一个新文件。")

        for filename in ("schema.sql", "seed.sql"):
            connection.executescript((DIRECTORY / filename).read_text(encoding="utf-8"))
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute("COMMIT")
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="创建会议室 SQLite 数据库并写入样例数据。")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database = parser.parse_args().database.resolve()
    try:
        created = initialize(database)
    except (ValueError, sqlite3.Error) as error:
        parser.error(str(error))
    print(f"{'已创建' if created else '已初始化'}：{database}")


if __name__ == "__main__":
    main()
