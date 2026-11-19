"""Read-only bounded disk statistics and capacity admission; never deletes files."""

import errno
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

from sleepscape.domain.maintenance import STATES, capacity_permission, capacity_state


class CapacityError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _existing(path: Path) -> Path:
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("unsafe_capacity_path")
    while not path.exists():
        path = path.parent
    return path


def _volume(path: Path, role: str) -> dict:
    info = os.statvfs(_existing(path))
    total = info.f_blocks * info.f_frsize
    free = info.f_bavail * info.f_frsize
    return {
        "role": role,
        "total_bytes": total,
        "free_bytes": free,
        "free_percent": round(100 * free / total, 2) if total else None,
        "state": capacity_state(total, free),
    }


def _size(path: Path) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("unsafe_capacity_file")
    return info.st_size


def _runtime_size(data_dir: Path) -> int | None:
    try:
        return sum(
            _size(data_dir / name)
            for name in (
                "model-runtime.json",
                "daily-runtime.json",
                "collection-cooldown.json",
                "maintenance-state.json",
            )
        )
    except (OSError, ValueError):
        # A runtime ledger has its own fail-closed validator; its size is not disk capacity.
        return None


def disk_snapshot(data_dir: Path) -> dict:
    """No recursive scans, database opens, state mutation or private paths in results."""
    result = {
        "state": "unknown",
        "measured_at": datetime.now(UTC).isoformat(),
        "volumes": [],
        "bytes": {},
        "error_code": None,
    }
    try:
        data_dir = Path(data_dir)
        volumes = [_volume(Path("/"), "system"), _volume(data_dir, "data")]
        result.update(
            state=max((v["state"] for v in volumes), key=STATES.__getitem__),
            volumes=volumes,
            bytes={
                "database": _size(data_dir / "sleepscape.sqlite3"),
                "wal": _size(data_dir / "sleepscape.sqlite3-wal"),
                "shm": _size(data_dir / "sleepscape.sqlite3-shm"),
                "runtime": _runtime_size(data_dir),
            },
        )
    except (OSError, ValueError, TypeError, OverflowError):
        result.update(state="unknown", error_code="storage_capacity_unavailable")
    return result


def require_capacity(data_dir: Path, operation: str, required_bytes: int = 0) -> dict:
    snapshot = disk_snapshot(data_dir)
    code = capacity_permission(snapshot, operation, required_bytes)
    if code:
        raise CapacityError(code)
    return snapshot


def is_disk_full(error: BaseException) -> bool:
    """Handle actual write failure even when a concurrent preflight saw free space."""
    return (isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}) or (
        isinstance(error, sqlite3.Error)
        and getattr(error, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_FULL
    )
