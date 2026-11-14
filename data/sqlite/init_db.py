import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent
DEFAULT_DATABASE = DIRECTORY / "meeting.db"


def initialize(database: Path) -> bool:
    """Create the schema and sample records once. Return whether they were created."""
    with closing(sqlite3.connect(database, autocommit=True)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            return False
        if version != 0:
            raise ValueError(f"Unsupported database version: {version}")

        for filename in ("schema.sql", "seed.sql"):
            connection.executescript((DIRECTORY / filename).read_text(encoding="utf-8"))
        connection.execute("PRAGMA user_version = 1")
        connection.execute("COMMIT")
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the SQLite meeting database.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database = parser.parse_args().database.resolve()
    created = initialize(database)
    print(f"{'Created' if created else 'Already initialized'}: {database}")


if __name__ == "__main__":
    main()
