"""One current rating per wake date; opening a reader never creates a database."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sleepscape.domain.morning_feeling import Feeling
from sleepscape.storage.reader import DashboardReader
from sleepscape.storage.usage import require_capacity

VALUES = ", ".join(f"'{value}'" for value in Feeling)
SCHEMA = f"""CREATE TABLE morning_feeling (
    night_date TEXT NOT NULL PRIMARY KEY,
    feeling TEXT NOT NULL CHECK(feeling IN ({VALUES})),
    filled_at TEXT NOT NULL
)"""


def available(connection):
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name='morning_feeling'"
    ).fetchone()
    if row is None:
        return False
    if row[0] != SCHEMA:
        raise ValueError("morning_feeling_schema_invalid")
    return True


def validate(connection):
    if not available(connection):
        raise ValueError("morning_feeling_not_prepared")
    for day, value, stamp in connection.execute(
        "SELECT night_date,feeling,filled_at FROM morning_feeling"
    ):
        if date.fromisoformat(day).isoformat() != day or datetime.fromisoformat(
            stamp
        ).utcoffset() != timedelta(0):
            raise ValueError("morning_feeling_record_invalid")
        Feeling(value)


def _writer(directory: Path):
    require_capacity(directory, "write")
    path = directory / "sleepscape.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise ValueError("morning_feeling_database_unavailable")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def migrate(directory):
    with closing(_writer(directory)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if not available(connection):
            connection.execute(SCHEMA)


def _record(connection, day):
    row = connection.execute(
        "SELECT night_date, feeling, filled_at FROM morning_feeling WHERE night_date=?",
        (day.isoformat(),),
    ).fetchone()
    return dict(row) if row else None


def read(directory, day):
    with DashboardReader(directory) as reader:
        if reader.connection is None:
            raise ValueError("morning_feeling_not_prepared")
        return _record(reader.connection, day)


def save(directory, day, feeling):
    with closing(_writer(directory)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO morning_feeling(night_date, feeling, filled_at) VALUES(?,?,?)
               ON CONFLICT(night_date) DO UPDATE SET feeling=excluded.feeling,
               filled_at=excluded.filled_at WHERE morning_feeling.feeling<>excluded.feeling""",
            (day.isoformat(), feeling.value, datetime.now(UTC).isoformat()),
        )
        return _record(connection, day)
