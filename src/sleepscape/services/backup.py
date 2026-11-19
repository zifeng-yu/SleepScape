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
from sleepscape.domain.environment_integrity import validate_environment_row
from sleepscape.storage import morning_feeling
from sleepscape.storage.analysis import ANALYSIS_TABLES, analysis_available, validate_analysis
from sleepscape.storage.environment import ENVIRONMENT_TABLES, PAYLOAD_TABLES
from sleepscape.storage.environment_index import INDEX_TABLES, index_available, validate_index
from sleepscape.storage.reports import write_private_json
from sleepscape.storage.sleep_analysis import (
    REPORT_TABLES,
    REVISION_TABLE,
    report_available,
    revisions_available,
    validate_reports,
)
from sleepscape.storage.usage import CapacityError, is_disk_full, require_capacity

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


def source_size(path: Path) -> int:
    """Include committed WAL growth in conservative copy space admission."""
    total = 0
    for candidate in (path, Path(str(path) + "-wal")):
        if candidate.is_symlink():
            raise BackupError("backup_source_unavailable")
        if candidate.exists():
            if not candidate.is_file():
                raise BackupError("backup_source_unavailable")
            total += candidate.stat().st_size
    return total


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
    present = set(ENVIRONMENT_TABLES) & tables
    if present and present != set(ENVIRONMENT_TABLES):
        raise BackupError("backup_environment_schema_incomplete")
    if present and connection.execute("SELECT version FROM environment_schema").fetchall() != [
        (1,)
    ]:
        raise BackupError("backup_environment_schema_unsupported")
    index_present = set(INDEX_TABLES) & tables
    if index_present:
        if not present:
            raise BackupError("backup_environment_index_without_facts")
        try:
            index_available(connection)
        except Exception:
            raise BackupError("backup_environment_index_schema_invalid") from None
    try:
        analysis_present = analysis_available(connection)
    except Exception:
        raise BackupError("backup_analysis_schema_invalid") from None
    try:
        reports_present = report_available(connection)
        revisions = revisions_available(connection)
        if revisions and not reports_present:
            raise BackupError("backup_sleep_analysis_schema_invalid")
    except Exception:
        raise BackupError("backup_sleep_analysis_schema_invalid") from None
    selected = (
        TABLES
        + (("morning_feeling",) if "morning_feeling" in tables else ())
        + (ENVIRONMENT_TABLES if present else ())
        + (INDEX_TABLES if index_present else ())
        + (ANALYSIS_TABLES if analysis_present else ())
        + (REPORT_TABLES if reports_present else ())
        + ((REVISION_TABLE,) if revisions else ())
    )
    return {
        name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in selected
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
    if "morning_feeling" in counts:
        try:
            morning_feeling.validate(connection)
        except (ValueError, TypeError, sqlite3.Error):
            raise BackupError("backup_morning_feeling_invalid") from None
    if "environment_schema" in counts:
        for table in PAYLOAD_TABLES:
            cursor = connection.execute(f"SELECT * FROM {table}")
            columns = [column[0] for column in cursor.description]
            for row in cursor:
                try:
                    validate_environment_row(table, dict(zip(columns, row, strict=True)))
                except (ValueError, TypeError, KeyError, RecursionError):
                    raise BackupError("backup_environment_payload_invalid") from None
    if "environment_index_schema" in counts:
        try:
            validate_index(connection)
        except Exception:
            raise BackupError("backup_environment_index_invalid") from None
    if "analysis_schema" in counts:
        try:
            validate_analysis(connection)
        except Exception:
            raise BackupError("backup_analysis_invalid") from None
    if "sleep_analysis_schema" in counts:
        try:
            validate_reports(connection)
        except Exception:
            raise BackupError("backup_sleep_analysis_invalid") from None
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
        if source_path.is_file() and not source_path.is_symlink():
            require_capacity(settings.data_dir, "backup", source_size(source_path) * 2)
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
    except CapacityError:
        raise BackupError("backup_capacity_blocked") from None
    except Exception as exc:
        if is_disk_full(exc):
            raise BackupError("backup_storage_full") from None
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
        if path.is_file() and not path.is_symlink():
            require_capacity(settings.data_dir, "backup", source_size(path) * 2)
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".sleepscape-restore-", dir=settings.data_dir) as directory:
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
    except CapacityError:
        raise BackupError("backup_capacity_blocked") from None
    except Exception as exc:
        if is_disk_full(exc):
            raise BackupError("backup_storage_full") from None
        raise BackupError("backup_restore_failed") from None
