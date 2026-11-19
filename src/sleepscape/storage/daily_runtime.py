"""Bounded private collection metadata; atomic files, shared mutex and read-only recovery."""

import fcntl
import json
import os
import re
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from sleepscape.domain.daily import ERROR_CODES, PHASES, STATUSES, ZONE

STATE_FILE = "daily-runtime.json"
COOLDOWN_FILE = "collection-cooldown.json"
LOCK_FILE = ".collection.lock"
MAX_DAYS = 31
MAX_BYTES = 128 * 1024
_ID = re.compile(r"[a-f0-9]{32}")
_FIELDS = {
    "day",
    "status",
    "phase",
    "attempt",
    "started_at",
    "finished_at",
    "run_id",
    "window",
    "request_counts",
    "error_code",
    "stop_reason",
    "retry_not_before",
    "analysis",
}


class DailyRuntimeError(Exception):
    def __init__(self, code="daily_state_unavailable"):
        self.code = code
        super().__init__(code)


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _directory(path, *, create=False):
    absolute = Path(path).absolute()
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise ValueError
    if not absolute.exists():
        if not create:
            return False
        absolute.mkdir(parents=True, mode=0o700)
    mode = absolute.stat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
        raise ValueError
    return True


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError
        obj[key] = value
    return obj


def _validate(state):
    if (
        not isinstance(state, dict)
        or set(state) != {"schema_version", "days"}
        or type(state["schema_version"]) is not int
        or state["schema_version"] != 1
        or not isinstance(state["days"], list)
        or len(state["days"]) > MAX_DAYS
    ):
        raise ValueError
    previous = None
    for row in state["days"]:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ValueError
        day = date.fromisoformat(row["day"])
        start = _time(row["started_at"])
        if previous and day <= previous:
            raise ValueError
        previous = day
        if day != start.astimezone(ZONE).date() or row["status"] not in STATUSES:
            raise ValueError
        if row["phase"] not in PHASES or row["analysis"] not in {
            "skipped_deferred",
            "separate_report_stage",
        }:
            raise ValueError
        if type(row["attempt"]) is not int or not 1 <= row["attempt"] <= 1_000_000:
            raise ValueError
        if row["run_id"] is not None and (
            not isinstance(row["run_id"], str) or not _ID.fullmatch(row["run_id"])
        ):
            raise ValueError
        if row["status"] == "running":
            if row["finished_at"] is not None:
                raise ValueError
        elif _time(row["finished_at"]) < start:
            raise ValueError
        if set(row["window"]) != {"start_date", "end_date"}:
            raise ValueError
        first = date.fromisoformat(row["window"]["start_date"])
        last = date.fromisoformat(row["window"]["end_date"])
        if last != day or not 0 <= (last - first).days <= 30:
            raise ValueError
        counts = row["request_counts"]
        if set(counts) != {"planned", "completed", "failed", "http"}:
            raise ValueError
        for key, value in counts.items():
            if key == "http" and value is None:
                continue
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise ValueError
        for key in ("error_code", "stop_reason"):
            if row[key] is not None and row[key] not in ERROR_CODES:
                raise ValueError
        if row["retry_not_before"] is not None:
            _time(row["retry_not_before"])
    return state


def _read(data_dir, name):
    if not _directory(data_dir):
        return None
    try:
        fd = os.open(data_dir / name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError
    return json.loads(raw, object_pairs_hook=_pairs)


def read_state(data_dir):
    """Never creates directories, repairs data, migrates SQLite or reads credentials."""
    try:
        value = _read(Path(data_dir), STATE_FILE)
        return _validate(value if value is not None else {"schema_version": 1, "days": []})
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise DailyRuntimeError() from None


def _write(data_dir, name, state):
    _directory(data_dir, create=True)
    path = data_dir / name
    if path.is_symlink():
        raise ValueError
    encoded = json.dumps(state, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError
    temporary = data_dir / f".{name}-{uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(data_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_state(data_dir, state):
    """Caller holds collection_mutex for the entire admission and collection attempt."""
    try:
        _write(Path(data_dir), STATE_FILE, _validate(state))
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise DailyRuntimeError() from None


def cooldown(data_dir):
    try:
        value = _read(Path(data_dir), COOLDOWN_FILE)
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"retry_not_before"}:
            raise ValueError
        return _time(value["retry_not_before"])
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise DailyRuntimeError() from None


def defer_until(data_dir, until):
    """Persist before another request, including manual collection and interrupted runs."""
    try:
        until = _time(until)
        previous = cooldown(data_dir)
        _write(
            Path(data_dir),
            COOLDOWN_FILE,
            {"retry_not_before": max(until, previous or until).isoformat()},
        )
    except (OSError, ValueError, TypeError, KeyError):
        raise DailyRuntimeError() from None


@contextmanager
def collection_mutex(data_dir):
    fd = None
    try:
        _directory(data_dir, create=True)
        fd = os.open(Path(data_dir) / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DailyRuntimeError("collection_already_running") from None
    except DailyRuntimeError:
        if fd is not None:
            os.close(fd)
        raise
    except (OSError, ValueError, TypeError):
        if fd is not None:
            os.close(fd)
        raise DailyRuntimeError() from None
    try:
        yield
    finally:
        os.close(fd)


def collection_busy(data_dir):
    """Read-only lock probe; never creates a lock file."""
    try:
        if not _directory(data_dir):
            return False
        try:
            fd = os.open(Path(data_dir) / LOCK_FILE, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise DailyRuntimeError() from None


def collection_result(data_dir, run_id):
    """Reconcile a crash after SQLite commit but before the atomic ledger update."""
    if run_id is None:
        return None
    try:
        path = Path(data_dir) / "sleepscape.sqlite3"
        if path.is_symlink():
            raise ValueError
        if not path.exists():
            return None
        connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
        with closing(connection):
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT status, summary_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None or row[0] == "running":
            return None
        summary = json.loads(row[1]) if row[1] else {}
        return {**summary, "status": row[0]}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise DailyRuntimeError() from None
