"""Private JSON report persistence, separate from report calculation and UI views."""

import json
import os
import stat
from pathlib import Path
from uuid import uuid4


def write_private_json(path: Path, value: dict) -> None:
    """Atomically publish a private report, leaving no partial output on failure."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.parent / f".{path.name}-{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_private_json(path: Path, *, max_bytes: int = 4096) -> dict:
    """Read bounded metadata only; callers select which fields may reach a browser."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("report_requires_regular_file")
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("report_size_exceeded")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("report_requires_object")
    return value
