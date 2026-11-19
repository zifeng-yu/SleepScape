"""Explicit index preparation and private, read-only-source replay verification."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sleepscape.config import Settings
from sleepscape.services.backup import TABLES, _copy, _validate, check_restore, create_backup
from sleepscape.storage.environment import ENVIRONMENT_TABLES, EnvironmentError
from sleepscape.storage.environment_index import INDEX_TABLES, migrate_index, rebuild_index
from sleepscape.storage.reports import write_private_json

FACT_TABLES = TABLES + ENVIRONMENT_TABLES


def _source(data_dir: Path) -> sqlite3.Connection:
    path = Path(data_dir) / "sleepscape.sqlite3"
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise EnvironmentError("environment_index_source_unavailable")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _present(connection: sqlite3.Connection) -> bool:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not set(FACT_TABLES) <= tables:
        raise EnvironmentError("environment_index_source_schema_invalid")
    present = set(INDEX_TABLES) & tables
    if present and present != set(INDEX_TABLES):
        raise EnvironmentError("environment_index_schema_incomplete")
    return bool(present)


def _fingerprints(connection: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    result = {}
    for table in FACT_TABLES:
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            for cell in row:
                value = (
                    b"blob:" + cell
                    if isinstance(cell, bytes)
                    else b"json:" + json.dumps(cell, ensure_ascii=False).encode("utf-8")
                )
                digest.update(len(value).to_bytes(8, "big"))
                digest.update(value)
            digest.update(b"\x00row\x00")
            count += 1
        result[table] = (count, digest.hexdigest())
    return result


def _projection(connection: sqlite3.Connection) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    columns = (
        "device_id,boot_id,record_type,seq,profile_id,utc_start_us,utc_end_us,"
        "time_quality,time_uncertainty_ms,anchor_ids_json,quality_json"
    )
    for row in connection.execute(
        f"SELECT {columns} FROM environment_time_index ORDER BY device_id,boot_id,record_type,seq"
    ):
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def prepare_environment_index(settings: Settings, force: bool = False) -> dict:
    """Only an explicit command calls this; first migration/force requires a verified backup."""
    try:
        with closing(_source(settings.data_dir)) as source:
            present = _present(source)
        backed_up = not present or force
        if backed_up:
            saved = create_backup(settings)
            check_restore(settings, Path(saved["backup_path"]))
        schema = migrate_index(settings.data_dir)
        result = rebuild_index(settings.data_dir, force=force)
        return {
            "status": result["status"],
            "backup_verified": backed_up,
            "schema": schema,
            "index": result,
        }
    except EnvironmentError:
        raise
    except Exception:
        raise EnvironmentError("environment_index_prepare_failed") from None


def verify_environment_index(settings: Settings) -> dict:
    """Verify real local facts in a disposable shadow, publishing only safe summary metadata."""
    metadata = {
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode": "shadow_rebuild",
        "fact_scope": "garmin_and_environment_original_tables",
    }
    report = {"status": "failed", "code": "environment_index_verification_failed"}
    try:
        with closing(_source(settings.data_dir)) as source:
            source.execute("BEGIN")
            present = _present(source)
            original = _fingerprints(source)
            current = (
                present
                and not source.execute(
                    "SELECT 1 FROM environment_index_boots "
                    "WHERE source_revision != indexed_revision "
                    "OR anchor_revision != indexed_anchor_revision LIMIT 1"
                ).fetchone()
            )
            previous = _projection(source) if current else None
            with TemporaryDirectory(prefix="sleepscape-environment-index-") as directory:
                temporary = Path(directory)
                temporary.chmod(0o700)
                copied = temporary / "sleepscape.sqlite3"
                _copy(source, copied)
                source.rollback()
                with closing(_source(temporary)) as shadow:
                    _validate(shadow)
                    if _fingerprints(shadow) != original:
                        raise EnvironmentError("environment_index_snapshot_mismatch")
                migrate_index(temporary)
                rebuild_index(temporary, force=True)
                with closing(_source(temporary)) as shadow:
                    first = _projection(shadow)
                    if _fingerprints(shadow) != original:
                        raise EnvironmentError("environment_index_fact_changed")
                rebuild_index(temporary, force=True)
                with closing(_source(temporary)) as shadow:
                    _validate(shadow)
                    repeated = _projection(shadow)
                    if _fingerprints(shadow) != original:
                        raise EnvironmentError("environment_index_fact_changed")
                    if repeated != first or (previous is not None and previous != first):
                        raise EnvironmentError("environment_index_replay_mismatch")
                    dirty = shadow.execute(
                        "SELECT COUNT(*) FROM environment_index_boots "
                        "WHERE source_revision != indexed_revision "
                        "OR anchor_revision != indexed_anchor_revision"
                    ).fetchone()[0]
                    if dirty or first[0] != original["environment_records"][0]:
                        raise EnvironmentError("environment_index_replay_incomplete")
                report = {
                    "status": "verified",
                    "source_read_only": True,
                    "source_index_present": present,
                    "source_index_current": bool(current),
                    "source_index_matches": previous == first if previous is not None else None,
                    "fact_table_count": len(original),
                    "fact_row_count": sum(value[0] for value in original.values()),
                    "indexed_record_count": first[0],
                    "facts_unchanged": True,
                    "repeated_rebuild_consistent": True,
                }
            report["temporary_copy_removed"] = not temporary.exists()
    except Exception:
        # Source content, identity, SQL errors and filesystem paths are never report fields.
        report = {"status": "failed", "code": "environment_index_verification_failed"}
    report.update(metadata)
    try:
        reports = settings.data_dir / "reports"
        if settings.data_dir.is_symlink() or reports.is_symlink():
            raise EnvironmentError("environment_index_report_path_invalid")
        if not settings.data_dir.is_dir():
            raise EnvironmentError("environment_index_source_unavailable")
        write_private_json(reports / "environment-index-verification.json", report)
    except Exception:
        raise EnvironmentError("environment_index_report_failed") from None
    return report
