"""Bounded private model metadata and durable shared request budget, never health data."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from sleepscape.models.audit import safe_usage
from sleepscape.models.base import ERROR_CODES, ModelError
from sleepscape.storage.usage import CapacityError, require_capacity

DAILY_LIMIT = 30
RETENTION_DAYS = 30
# Normal use needs 900 rows. A bounded margin also retains prior explicitly
# authorized verification calls without rewriting their historical audit rows.
MAX_ATTEMPTS = 1024
MAX_STATE_BYTES = 1024 * 1024
ZONE = ZoneInfo("Asia/Shanghai")
STATE_FILE = "model-runtime.json"
LOCK_FILE = ".model-runtime.lock"
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[a-f0-9]{32}$")
_FIELDS = {
    "id",
    "day",
    "started_at",
    "finished_at",
    "fingerprint",
    "status",
    "code",
    "http_status",
    "retry_after",
    "usage",
    "duration_ms",
}


def _time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError
    return instant.astimezone(UTC)


def instant(now: datetime) -> str:
    if now.tzinfo is None:
        raise ModelError("model_state_unavailable")
    return now.astimezone(UTC).isoformat()


def _directory(path: Path, *, create: bool = False) -> bool:
    # Check the complete path rather than resolving symlinks away.
    absolute = path.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise ModelError("model_state_unavailable")
    if not absolute.exists():
        if not create:
            return False
        absolute.mkdir(parents=True, mode=0o700)
    mode = absolute.stat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
        raise ModelError("model_state_unavailable")
    return True


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _validate(value: object) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "attempts"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["attempts"], list)
        or len(value["attempts"]) > MAX_ATTEMPTS
    ):
        raise ValueError
    identities = set()
    previous = None
    for row in value["attempts"]:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ValueError
        if not isinstance(row["id"], str) or not _ID.fullmatch(row["id"]):
            raise ValueError
        if row["id"] in identities:
            raise ValueError
        identities.add(row["id"])
        if not isinstance(row["fingerprint"], str) or not _FINGERPRINT.fullmatch(
            row["fingerprint"]
        ):
            raise ValueError
        start = _time(row["started_at"])
        if previous and start < previous:
            raise ValueError
        previous = start
        if row["day"] != start.astimezone(ZONE).date().isoformat():
            raise ValueError
        # Historical physical calls remain facts even if an explicitly authorized
        # temporary verification exceeded today's normal admission limit. The
        # current reserve() path enforces DAILY_LIMIT and counts every prior row.
        if row["status"] not in {"running", "verified", "failed"}:
            raise ValueError
        if row["status"] == "running":
            if row["finished_at"] is not None or row["code"] is not None:
                raise ValueError
        elif _time(row["finished_at"]) < start:
            raise ValueError
        if row["code"] is not None and row["code"] not in ERROR_CODES:
            raise ValueError
        if row["status"] == "verified" and row["code"] is not None:
            raise ValueError
        if row["status"] == "failed" and row["code"] is None:
            raise ValueError
        if row["http_status"] is not None and (
            type(row["http_status"]) is not int or not 100 <= row["http_status"] <= 599
        ):
            raise ValueError
        if row["retry_after"] is not None and (
            type(row["retry_after"]) is not int or not 0 <= row["retry_after"] <= 86400
        ):
            raise ValueError
        if not isinstance(row["usage"], dict) or safe_usage(row["usage"]) != row["usage"]:
            raise ValueError
        if row["duration_ms"] is not None and (
            type(row["duration_ms"]) is not int or not 0 <= row["duration_ms"] <= 300_000
        ):
            raise ValueError
    return value


def read_state(data_dir: Path) -> dict:
    """GET-safe: no mkdir, chmod, repair, credential read, or network."""
    try:
        if not _directory(data_dir):
            return {"schema_version": 1, "attempts": []}
        path = data_dir / STATE_FILE
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"schema_version": 1, "attempts": []}
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_no_duplicates)
        return _validate(value)
    except ModelError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ModelError("model_state_unavailable") from None


def budget(state: dict, now: datetime) -> dict:
    day = now.astimezone(ZONE).date().isoformat()
    if any(_time(row["started_at"]) > now.astimezone(UTC) for row in state["attempts"]):
        raise ModelError("model_state_unavailable")
    used = sum(row["day"] == day for row in state["attempts"])
    return {"limit": DAILY_LIMIT, "used": used, "remaining": max(0, DAILY_LIMIT - used)}


def _write(data_dir: Path, state: dict) -> None:
    try:
        _validate(state)
        _directory(data_dir)
        path = data_dir / STATE_FILE
        if path.is_symlink():
            raise ValueError
        encoded = json.dumps(state, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > MAX_STATE_BYTES:
            raise ValueError
        temporary = data_dir / f".{STATE_FILE}-{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(data_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    except ModelError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        raise ModelError("model_state_unavailable") from None


class RuntimeSession:
    """Created only while the independent process lock is held."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.state = read_state(data_dir)

    def reserve(self, fingerprint: str, now: datetime) -> str:
        try:
            require_capacity(self.data_dir, "model")
        except CapacityError as exc:
            raise ModelError(exc.code) from None
        if budget(self.state, now)["remaining"] == 0:
            raise ModelError("budget_exhausted")
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise ModelError("model_configuration_invalid")
        if self.state["attempts"]:
            latest = self.state["attempts"][-1]
            if latest["retry_after"] and latest["finished_at"]:
                retry_at = _time(latest["finished_at"]) + timedelta(seconds=latest["retry_after"])
                if now.astimezone(UTC) < retry_at:
                    raise ModelError("rate_limited", retry_after=latest["retry_after"])
        earliest = now.astimezone(ZONE).date() - timedelta(days=RETENTION_DAYS - 1)
        self.state["attempts"] = [
            row for row in self.state["attempts"] if row["day"] >= earliest.isoformat()
        ]
        for row in self.state["attempts"]:
            if row["status"] == "running":
                row.update(status="failed", code="model_worker_failed", finished_at=instant(now))
        attempt_id = uuid4().hex
        self.state["attempts"].append(
            {
                "id": attempt_id,
                "day": now.astimezone(ZONE).date().isoformat(),
                "started_at": instant(now),
                "finished_at": None,
                "fingerprint": fingerprint,
                "status": "running",
                "code": None,
                "http_status": None,
                "retry_after": None,
                "usage": {},
                "duration_ms": None,
            }
        )
        _write(self.data_dir, self.state)
        return attempt_id

    def finish(
        self,
        attempt_id: str,
        now: datetime,
        duration_ms: int,
        *,
        error: ModelError | None = None,
        usage: dict | None = None,
    ) -> None:
        row = next((row for row in self.state["attempts"] if row["id"] == attempt_id), None)
        if row is None or row["status"] != "running":
            raise ModelError("model_state_unavailable")
        end = max(now.astimezone(UTC), _time(row["started_at"]))
        row.update(
            status="failed" if error else "verified",
            finished_at=instant(end),
            duration_ms=min(300_000, max(0, duration_ms)),
            code=error.code if error else None,
            http_status=error.http_status if error else None,
            retry_after=error.retry_after if error else None,
            usage=safe_usage(usage),
        )
        _write(self.data_dir, self.state)


@contextmanager
def session(data_dir: Path):
    """Single model caller across processes; no Garmin or database lock is held."""
    descriptor = None
    try:
        _directory(data_dir, create=True)
        descriptor = os.open(
            data_dir / LOCK_FILE,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ModelError("model_state_unavailable")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ModelError("model_busy") from None
        yield RuntimeSession(data_dir)
    except ModelError:
        raise
    except OSError:
        raise ModelError("model_state_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
