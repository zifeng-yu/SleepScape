"""Consistent private SQLite backups and disposable restore checks; no network."""

import hashlib
import json
import os
import sqlite3
import time
import zlib
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sleepscape.config import Settings
from sleepscape.storage.reports import write_private_json

TABLES = ("runs", "raw_snapshots", "normalized_records", "requests")
MAX_RAW_BYTES = 64 * 1024 * 1024


class BackupError(Exception):
    """Expose static codes only, never SQLite messages or source contents."""

    def __init__(self, code: str):
        super().__init__(code)
        self.__suppress_context__ = True


def _readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise BackupError("backup_source_unavailable")
    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    source.execute("PRAGMA query_only = ON")
    return source


def _copy(source: sqlite3.Connection, destination: Path) -> None:
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    started = time.monotonic()

    def progress(status, remaining, total):
        if time.monotonic() - started > 120:
            raise BackupError("backup_copy_timeout")

    with closing(sqlite3.connect(destination)) as target:
        source.backup(target, pages=256, progress=progress, sleep=0.05)
        # A backup must be a standalone file, not depend on an unshipped WAL.
        target.execute("PRAGMA journal_mode = DELETE")
    destination.chmod(0o600)


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not set(TABLES) <= tables:
        raise BackupError("backup_schema_invalid")
    return {
        name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES
    }


def _validate(connection: sqlite3.Connection) -> dict[str, int]:
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise BackupError("backup_integrity_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupError("backup_foreign_keys_failed")
    counts = _counts(connection)
    for content_hash, compressed in connection.execute(
        "SELECT content_hash, payload_zlib FROM raw_snapshots"
    ):
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, MAX_RAW_BYTES + 1)
            if len(raw) > MAX_RAW_BYTES or not decoder.eof or decoder.unused_data:
                raise BackupError("backup_raw_size_or_encoding_invalid")
            if hashlib.sha256(raw).hexdigest() != content_hash:
                raise BackupError("backup_raw_hash_failed")
            json.loads(raw)
        except (TypeError, ValueError, zlib.error):
            raise BackupError("backup_raw_json_invalid") from None
    return counts


def _status(settings: Settings, counts: dict[str, int]) -> None:
    value = {
        "last_success_at": datetime.now(UTC).isoformat(),
        "status": "verified",
        "record_count": counts["normalized_records"],
    }
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.data_dir.chmod(0o700)
    write_private_json(settings.data_dir / "reports" / "backup-status.json", value)


def _remove_temporary(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


def create_backup(settings: Settings) -> dict:
    """Publish a new verified backup without overwriting existing backups."""
    temporary = None
    try:
        source_path = settings.data_dir / "sleepscape.sqlite3"
        with closing(_readonly(source_path)) as source:
            directory = settings.data_dir / "backups"
            if directory.is_symlink():
                raise BackupError("backup_directory_symlink_not_allowed")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
            name = f"sleepscape-{uuid4().hex}.sqlite3"
            destination = directory / name
            if destination.exists() or destination.is_symlink():
                raise BackupError("backup_name_collision")
            temporary = directory / f".{name}.tmp"
            _copy(source, temporary)
        with closing(_readonly(temporary)) as copy:
            counts = _validate(copy)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        # link creates the verified destination atomically and refuses overwrite.
        os.link(temporary, destination)
        _remove_temporary(temporary)
        temporary = None
        _status(settings, counts)
        return {
            "status": "verified",
            "backup_path": str(Path("backups") / name),
            "counts": counts,
            "record_count": counts["normalized_records"],
        }
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_failed") from None
    finally:
        if temporary is not None:
            try:
                _remove_temporary(temporary)
            except OSError:
                raise BackupError("backup_temporary_cleanup_failed") from None


def check_restore(settings: Settings, backup: Path) -> dict:
    """Restore into a private disposable directory, never the live database."""
    try:
        path = backup if backup.is_absolute() else settings.data_dir / backup
        with TemporaryDirectory(prefix="sleepscape-restore-") as directory:
            temporary = Path(directory)
            temporary.chmod(0o700)
            restored = temporary / "restored.sqlite3"
            with closing(_readonly(path)) as source:
                source.execute("BEGIN")
                expected = _counts(source)
                _copy(source, restored)
            with closing(_readonly(restored)) as copy:
                counts = _validate(copy)
            if counts != expected:
                raise BackupError("backup_restore_counts_mismatch")
        _status(settings, counts)
        return {
            "status": "verified",
            "counts": counts,
            "record_count": counts["normalized_records"],
            "source_counts_match": True,
            "temporary_copy_removed": not temporary.exists(),
        }
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_restore_failed") from None
