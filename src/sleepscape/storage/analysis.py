"""Explicit readiness persistence; private audit snapshots and a current projection.

No report tables or model calls belong here. Reading never creates a database or
schema. The caller verifies a backup before invoking the explicit migration.
"""

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from sleepscape.domain.analysis_readiness import READABLE_COVERAGE_POLICIES
from sleepscape.storage.repository import secure_database_files
from sleepscape.storage.usage import CapacityError, is_disk_full, require_capacity

ANALYSIS_VERSION = 1
ANALYSIS_TABLES = ("analysis_schema", "analysis_readiness_checks", "analysis_readiness")
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_BATCH_RESULTS = 31
MAX_READ_DATES = 92
_BASE_TABLES = {"runs", "raw_snapshots", "normalized_records", "requests"}
_BASE_FIELDS = {
    "policy_version",
    "night_date",
    "checked_at",
    "data_state",
    "core_ready",
    "window",
    "missing",
    "optional_missing",
    "garmin_summary",
    "environment_summary",
    "limitations",
    "source_fingerprint",
}
# The old shape is an archival contract, not a second readiness algorithm.
_POLICY_FIELDS = {
    "dual-source-strict-1": _BASE_FIELDS,
    **{
        version: _BASE_FIELDS | {"policy", "quality_state", "warnings"}
        for version in READABLE_COVERAGE_POLICIES
    },
}
_CHANNELS = {"temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative"}
_CHANNEL_FIELDS = {
    "expected_windows",
    "valid_windows",
    "invalid_windows",
    "expected_count",
    "observed_count",
    "valid_count",
    "missing_count",
    "coverage_ratio",
    "missing_seconds",
    "effective_coverage_ratio",
    "usable_count",
    "max_affected_span_seconds",
}
_ENVIRONMENT_FIELDS = {
    "expected_windows",
    "observed_windows",
    "missing_windows",
    "missing_windows_lower_bound",
    "count_exact",
    "expected_seconds",
    "covered_seconds",
    "missing_seconds",
    "overlap_seconds",
    "gaps",
    "channels",
    "coverage_scope",
}
_ISSUE_FIELDS = {
    "code",
    "source",
    "metric",
    "date",
    "window_start",
    "window_end",
    "expected",
    "observed",
    "recoverability",
    "last_checked_at",
}
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DDL = {
    "analysis_schema": """CREATE TABLE analysis_schema (
        version INTEGER PRIMARY KEY, created_at TEXT NOT NULL)""",
    "analysis_readiness_checks": """CREATE TABLE analysis_readiness_checks (
        check_id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL, night_date TEXT NOT NULL, checked_at TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        data_state TEXT NOT NULL CHECK(data_state IN ('waiting_data','ready','expired_unanalyzed')),
        core_ready INTEGER NOT NULL CHECK(core_ready IN (0,1)),
        source_fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        UNIQUE(run_id,night_date), UNIQUE(night_date,check_id))""",
    "analysis_readiness": """CREATE TABLE analysis_readiness (
        night_date TEXT PRIMARY KEY, check_id INTEGER NOT NULL UNIQUE,
        FOREIGN KEY(night_date,check_id)
        REFERENCES analysis_readiness_checks(night_date,check_id))""",
    "analysis_readiness_checks_latest": """CREATE INDEX analysis_readiness_checks_latest
        ON analysis_readiness_checks(night_date,checked_at DESC,check_id DESC)""",
    "analysis_readiness_checks_no_update": """CREATE TRIGGER analysis_readiness_checks_no_update
        BEFORE UPDATE ON analysis_readiness_checks
        BEGIN SELECT RAISE(ABORT,'analysis_audit_immutable'); END""",
    "analysis_readiness_checks_no_delete": """CREATE TRIGGER analysis_readiness_checks_no_delete
        BEFORE DELETE ON analysis_readiness_checks
        BEGIN SELECT RAISE(ABORT,'analysis_audit_immutable'); END""",
}


class AnalysisStorageError(Exception):
    """Static errors only: never expose private snapshots, paths, or SQLite text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
        self.__suppress_context__ = True


def _fail(code="analysis_payload_invalid"):
    raise AnalysisStorageError(code)


def _day(value) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail()
    try:
        result = date.fromisoformat(value)
    except ValueError:
        _fail()
    if not 2000 <= result.year <= 2100:
        _fail()
    return value


def _instant(value) -> str:
    if not isinstance(value, str) or len(value) > 40:
        _fail()
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or not 2000 <= result.year <= 2100:
        _fail()
    return result.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _uuid(value) -> str:
    if not isinstance(value, str) or len(value) not in (32, 36):
        _fail("analysis_run_id_invalid")
    try:
        return str(UUID(value))
    except ValueError:
        _fail("analysis_run_id_invalid")


def _json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _bounded_json(value, *, depth=0, budget=None) -> None:
    """Accept only bounded JSON; storage does not duplicate readiness calculations."""
    if budget is None:
        budget = [250_000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 12:
        _fail()
    if value is None or type(value) is bool:
        return
    if type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > 1e18:
            _fail()
    elif isinstance(value, str):
        if len(value) > 2048:
            _fail()
    elif type(value) in (list, dict):
        if len(value) > (8192 if isinstance(value, list) else 128):
            _fail()
        if isinstance(value, dict) and any(
            not isinstance(key, str) or not _CODE.fullmatch(key) for key in value
        ):
            _fail()
        for item in value.values() if isinstance(value, dict) else value:
            _bounded_json(item, depth=depth + 1, budget=budget)
    else:
        _fail()


def _count(value, *, optional=False):
    if optional and value is None:
        return
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        _fail()


def _range(value, maximum, *, optional=False):
    if optional and value is None:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        _fail()


def _validate_coverage_summaries(value):
    """Validate the new serialized shape without recalculating sensor eligibility."""
    garmin = value["garmin_summary"]
    metrics = {
        "sleep_seconds",
        "average_heart_rate",
        "night_average_hrv",
        "training_load",
        "previous_stress",
    }
    states = {
        "available",
        "missing",
        "invalid",
        "stale",
        "pending",
        "unavailable",
        "date_mismatch",
        "source_mismatch",
        "conflict",
        "counts_incomplete",
    }
    if set(garmin) != {"required_count", "available_count", "window_state", "metrics"}:
        _fail()
    if type(garmin["required_count"]) is not int or garmin["required_count"] != 5:
        _fail()
    _count(garmin["available_count"])
    if garmin["available_count"] > 5 or garmin["window_state"] not in {
        "available",
        "missing",
        "invalid",
        "too_long",
        "not_closed",
        "conflict",
        "date_mismatch",
        "stale",
    }:
        _fail()
    if type(garmin["metrics"]) is not dict or set(garmin["metrics"]) != metrics:
        _fail()
    for metric in garmin["metrics"].values():
        if (
            type(metric) is not dict
            or set(metric) != {"state", "source_date"}
            or metric["state"] not in states
        ):
            _fail()
        if metric["source_date"] is not None:
            _day(metric["source_date"])
    summary, window = value["environment_summary"], value["window"]
    if window is None:
        if summary is not None:
            _fail()
        return
    if type(summary) is not dict or set(summary) != _ENVIRONMENT_FIELDS:
        _fail()
    if summary["coverage_scope"] != "fully_valid_windows_clipped_to_sleep":
        _fail()
    for key in ("expected_windows", "missing_windows"):
        _count(summary[key], optional=True)
    for key in ("observed_windows", "missing_windows_lower_bound"):
        _count(summary[key])
    if type(summary["count_exact"]) is not bool:
        _fail()
    duration = window["duration_seconds"]
    for key in ("expected_seconds", "covered_seconds", "missing_seconds", "overlap_seconds"):
        _range(summary[key], duration)
    if summary["expected_seconds"] != duration or type(summary["gaps"]) is not list:
        _fail()
    for gap in summary["gaps"]:
        if type(gap) is not dict or set(gap) != {"start", "end", "seconds"}:
            _fail()
        start, end = _instant(gap["start"]), _instant(gap["end"])
        if not _instant(window["start"]) <= start < end <= _instant(window["end"]):
            _fail()
        _range(gap["seconds"], duration)
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        if abs(seconds - gap["seconds"]) > 1e-6:
            _fail()
    if type(summary["channels"]) is not dict or set(summary["channels"]) != _CHANNELS:
        _fail()
    for name, channel in summary["channels"].items():
        if channel is None:
            if value["core_ready"]:
                _fail()
            continue
        fields = _CHANNEL_FIELDS | (
            {"audio_missing_seconds", "audio_missing_scope"} if name == "sound_relative" else set()
        )
        if type(channel) is not dict or set(channel) != fields:
            _fail()
        for key in ("expected_windows", "expected_count", "missing_count"):
            _count(channel[key], optional=True)
        for key in (
            "valid_windows",
            "invalid_windows",
            "observed_count",
            "valid_count",
            "usable_count",
        ):
            _count(channel[key])
        _range(channel["coverage_ratio"], 1)
        _range(channel["effective_coverage_ratio"], 1, optional=True)
        if (channel["expected_count"] is None) != (channel["effective_coverage_ratio"] is None):
            _fail()
        _range(channel["missing_seconds"], duration)
        _range(channel["max_affected_span_seconds"], duration)
        if name == "sound_relative":
            _range(
                channel["audio_missing_seconds"], summary["observed_windows"] * 60, optional=True
            )
            if channel["audio_missing_scope"] != "whole_observed_minutes":
                _fail()
        if value["core_ready"] and (
            channel["effective_coverage_ratio"] is None
            or channel["effective_coverage_ratio"] < value["policy"]["min_channel_coverage"]
            or channel["max_affected_span_seconds"] > value["policy"]["max_affected_span_seconds"]
        ):
            _fail()
        if value["core_ready"]:
            # Eligible summaries must agree internally even if their JSON hash was
            # recomputed. Blocked audits may retain inconsistent source counters.
            expected = channel["expected_count"]
            if (
                expected is None
                or expected <= 0
                or not 0
                <= channel["usable_count"]
                <= channel["valid_count"]
                <= channel["observed_count"]
                <= expected
                or channel["effective_coverage_ratio"] != channel["usable_count"] / expected
                or channel["missing_count"] != expected - channel["valid_count"]
            ):
                _fail()


def _validate_payload(value: dict) -> tuple[str, str]:
    try:
        if type(value) is not dict or not isinstance(value.get("policy_version"), str):
            _fail()
        policy_version = value["policy_version"]
        if policy_version not in _POLICY_FIELDS or set(value) != _POLICY_FIELDS[policy_version]:
            _fail()
        coverage = policy_version in READABLE_COVERAGE_POLICIES
        _bounded_json(value)
        _day(value["night_date"])
        checked_at = _instant(value["checked_at"])
        if not isinstance(value["policy_version"], str) or not _CODE.fullmatch(
            value["policy_version"]
        ):
            _fail()
        if not isinstance(value["source_fingerprint"], str) or not _HASH.fullmatch(
            value["source_fingerprint"]
        ):
            _fail()
        state = value["data_state"]
        if (
            state not in {"waiting_data", "ready", "expired_unanalyzed"}
            or type(value["core_ready"]) is not bool
        ):
            _fail()
        if (state == "ready" and not value["core_ready"]) or (
            state == "waiting_data" and value["core_ready"]
        ):
            _fail()
        for name in ("missing", "optional_missing", *(["warnings"] if coverage else [])):
            if not isinstance(value[name], list):
                _fail()
            for issue in value[name]:
                if not isinstance(issue, dict) or set(issue) != _ISSUE_FIELDS:
                    _fail()
                sources = {"garmin", "environment"}
                if name == "optional_missing":
                    sources = sources | {"manual"}
                elif name == "warnings":
                    sources = {"environment"}
                if issue["source"] not in sources:
                    _fail()
                if issue["recoverability"] not in {
                    "await_sync",
                    "await_upload",
                    "unrecoverable",
                    "unknown",
                }:
                    _fail()
                if any(
                    not isinstance(issue[k], str) or not _CODE.fullmatch(issue[k])
                    for k in ("code", "metric")
                ):
                    _fail()
                _day(issue["date"])
                _instant(issue["last_checked_at"])
                for key in ("window_start", "window_end"):
                    if issue[key] is not None:
                        _instant(issue[key])
                for key in ("expected", "observed"):
                    number = issue[key]
                    if number is not None and not (
                        type(number) in (int, float)
                        and number >= 0
                        or isinstance(number, str)
                        and _CODE.fullmatch(number)
                    ):
                        _fail()
        if bool(value["missing"]) == value["core_ready"]:
            _fail()
        if coverage:
            policy = value["policy"]
            if (
                type(policy) is not dict
                or set(policy) != {"min_channel_coverage", "max_affected_span_seconds"}
                or type(policy["min_channel_coverage"]) not in (int, float)
                or policy["min_channel_coverage"] != 0.99
                or type(policy["max_affected_span_seconds"]) is not int
                or policy["max_affected_span_seconds"] != 300
            ):
                _fail()
            quality = (
                "insufficient"
                if value["missing"]
                else "with_gaps"
                if value["warnings"]
                else "complete"
            )
            if value["quality_state"] != quality:
                _fail()
        if not isinstance(value["garmin_summary"], dict):
            _fail()
        if value["environment_summary"] is not None and not isinstance(
            value["environment_summary"], dict
        ):
            _fail()
        if not isinstance(value["limitations"], list) or any(
            not isinstance(code, str) or not _CODE.fullmatch(code) for code in value["limitations"]
        ):
            _fail()
        window = value["window"]
        if window is not None:
            if not isinstance(window, dict) or set(window) != {"start", "end", "duration_seconds"}:
                _fail()
            start, end = _instant(window["start"]), _instant(window["end"])
            if not start < end or type(window["duration_seconds"]) not in (int, float):
                _fail()
            if value["core_ready"] and end > checked_at:
                _fail()
            seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
            if not 0 < seconds <= 86400 or abs(seconds - window["duration_seconds"]) > 1e-6:
                _fail()
        elif value["core_ready"]:
            _fail()
        if coverage:
            _validate_coverage_summaries(value)
        encoded = _json(value)
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            _fail()
        return encoded, checked_at
    except AnalysisStorageError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError):
        _fail()


def analysis_available(connection: sqlite3.Connection) -> bool:
    """Validate the optional schema and immutable-audit guards without writing."""
    try:
        placeholders = ",".join("?" for _ in _DDL)
        objects = dict(
            connection.execute(
                f"SELECT name,sql FROM sqlite_master WHERE name IN ({placeholders})", tuple(_DDL)
            )
        )
        if not objects:
            return False
        if set(objects) != set(_DDL):
            _fail("analysis_schema_incomplete")

        def normalize(text):
            return " ".join(text.lower().split())

        if any(
            not objects[name] or normalize(objects[name]) != normalize(sql)
            for name, sql in _DDL.items()
        ):
            _fail("analysis_schema_invalid")
        rows = connection.execute("SELECT version,created_at FROM analysis_schema").fetchall()
        if len(rows) != 1 or rows[0][0] != ANALYSIS_VERSION:
            _fail("analysis_schema_unsupported")
        try:
            _instant(rows[0][1])
        except (AnalysisStorageError, ValueError):
            _fail("analysis_schema_invalid")
        return True
    except AnalysisStorageError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        _fail("analysis_schema_invalid")


@contextmanager
def _database(data_dir: Path, *, writable=False):
    path = Path(data_dir) / "sleepscape.sqlite3"
    connection = None
    try:
        if path.is_symlink() or path.parent.is_symlink():
            _fail("analysis_source_unavailable")
        if not path.exists() and not writable:
            yield None
            return
        if not path.is_file():
            _fail("analysis_source_unavailable")
        mode = "rw" if writable else "ro"
        connection = sqlite3.connect(path.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=3)
        connection.execute("PRAGMA foreign_keys=ON")
        if not writable:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not _BASE_TABLES <= tables:
            _fail("analysis_base_schema_invalid")
        yield connection
    finally:
        if connection is not None:
            connection.close()


def migrate_analysis(data_dir: Path) -> dict:
    """Explicit optional migration; a service must verify a backup before calling."""
    try:
        with _database(data_dir, writable=True) as connection:
            if analysis_available(connection):
                return {"status": "already_initialized", "schema_version": ANALYSIS_VERSION}
            # Reserve small schema/index pages and their WAL copy before creating objects.
            require_capacity(Path(data_dir), "write", 2 * 64 * 1024)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                # Another explicit command may have finished while we acquired the lock.
                if analysis_available(connection):
                    return {"status": "already_initialized", "schema_version": ANALYSIS_VERSION}
                for statement in _DDL.values():
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO analysis_schema VALUES (?,?)",
                    (
                        ANALYSIS_VERSION,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                analysis_available(connection)
        secure_database_files(data_dir)
        return {"status": "initialized", "schema_version": ANALYSIS_VERSION}
    except AnalysisStorageError:
        raise
    except CapacityError:
        _fail("analysis_capacity_blocked")
    except Exception as exc:
        _fail("analysis_storage_full" if is_disk_full(exc) else "analysis_migration_failed")


def _rows(cursor):
    columns = [column[0] for column in cursor.description]
    for row in cursor:
        yield dict(zip(columns, row, strict=True))


def _decode(row: dict) -> dict:
    try:
        raw = row["payload_json"]
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
            _fail()
        value = json.loads(raw)
        encoded, checked_at = _validate_payload(value)
        if (
            encoded != raw
            or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["payload_hash"]
        ):
            _fail("analysis_payload_hash_invalid")
        if (
            any(
                row[key] != value[key]
                for key in (
                    "night_date",
                    "policy_version",
                    "data_state",
                    "source_fingerprint",
                )
            )
            or row["checked_at"] != checked_at
            or row["core_ready"] != int(value["core_ready"])
        ):
            _fail("analysis_payload_projection_invalid")
        if (
            type(row["check_id"]) is not int
            or row["check_id"] <= 0
            or _uuid(row["run_id"]) != row["run_id"]
        ):
            _fail("analysis_payload_projection_invalid")
        return {**value, "check_id": row["check_id"], "run_id": row["run_id"]}
    except AnalysisStorageError:
        raise
    except (KeyError, ValueError, TypeError, RecursionError):
        _fail()


def validate_analysis(connection: sqlite3.Connection) -> None:
    """Backup validation checks snapshots, hashes, references and latest projections."""
    if not analysis_available(connection):
        return
    for row in _rows(connection.execute("SELECT * FROM analysis_readiness_checks")):
        _decode(row)
    invalid = connection.execute("""
        SELECT 1 FROM analysis_readiness r
        LEFT JOIN analysis_readiness_checks c
          ON c.night_date=r.night_date AND c.check_id=r.check_id
        WHERE c.check_id IS NULL OR r.check_id != (
          SELECT check_id FROM analysis_readiness_checks c2 WHERE c2.night_date=r.night_date
          ORDER BY checked_at DESC,check_id DESC LIMIT 1)
        UNION ALL
        SELECT 1 FROM analysis_readiness_checks c
        LEFT JOIN analysis_readiness r ON r.night_date=c.night_date
        WHERE r.check_id IS NULL LIMIT 1
    """).fetchone()
    if invalid:
        _fail("analysis_current_projection_invalid")


def _read_bulk(connection, days):
    if connection is None or not analysis_available(connection):
        return {}
    placeholders = ",".join("?" for _ in days)
    result = {}
    for row in _rows(
        connection.execute(
            f"""
        SELECT c.* FROM analysis_readiness r LEFT JOIN analysis_readiness_checks c
          ON c.check_id=r.check_id AND c.night_date=r.night_date
        WHERE r.night_date IN ({placeholders})
    """,
            days,
        )
    ):
        item = _decode(row)
        result[item["night_date"]] = item
    requested = ",".join("(?)" for _ in days)
    latest = {
        day: check_id
        for day, check_id in connection.execute(
            f"""
        WITH requested(day) AS (VALUES {requested})
        SELECT day,(SELECT check_id FROM analysis_readiness_checks WHERE night_date=day
          ORDER BY checked_at DESC,check_id DESC LIMIT 1) FROM requested
    """,
            days,
        )
        if check_id is not None
    }
    if latest != {day: item["check_id"] for day, item in result.items()}:
        _fail("analysis_current_projection_invalid")
    return result


def read_bulk(data_dir: Path, days, *, connection: sqlite3.Connection | None = None) -> dict:
    """Read at most 92 requested dates; an optional caller-owned snapshot stays open."""
    try:
        days = list(days)
        if len(days) > MAX_READ_DATES:
            _fail("analysis_date_range_invalid")
        days = list(dict.fromkeys(_day(day) for day in days))
        if not days:
            return {}
        if connection is not None:
            return _read_bulk(connection, days)
        with _database(data_dir) as source:
            return _read_bulk(source, days)
    except AnalysisStorageError:
        raise
    except Exception:
        _fail("analysis_read_failed")


def read_latest(
    data_dir: Path, night_date, *, connection: sqlite3.Connection | None = None
) -> dict | None:
    day = _day(night_date)
    return read_bulk(data_dir, [day], connection=connection).get(day)


def save_results(data_dir: Path, results: list[dict], *, run_id: str) -> dict:
    """Append a whole run atomically; retrying identical run/night snapshots is idempotent."""
    try:
        run_id = _uuid(run_id)
        if not isinstance(results, list) or not 1 <= len(results) <= MAX_BATCH_RESULTS:
            _fail("analysis_batch_invalid")
        prepared = [(value, *_validate_payload(value)) for value in results]
        days = [value["night_date"] for value in results]
        if len(set(days)) != len(days):
            _fail("analysis_batch_duplicate_night")
        require_capacity(
            Path(data_dir), "write", 2 * sum(len(raw.encode("utf-8")) for _, raw, _ in prepared)
        )
        inserted = updated = duplicates = 0
        with _database(data_dir, writable=True) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if not analysis_available(connection):
                _fail("analysis_not_initialized")
            # Reject existing corruption rather than quietly healing its projection.
            _read_bulk(connection, days)
            for value, encoded, checked_at in prepared:
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                existing = next(
                    _rows(
                        connection.execute(
                            "SELECT * FROM analysis_readiness_checks "
                            "WHERE run_id=? AND night_date=?",
                            (run_id, value["night_date"]),
                        )
                    ),
                    None,
                )
                if existing:
                    _decode(existing)
                    if existing["payload_hash"] != digest:
                        _fail("analysis_run_conflict")
                    duplicates += 1
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO analysis_readiness_checks
                    (run_id,night_date,checked_at,policy_version,data_state,core_ready,
                     source_fingerprint,payload_json,payload_hash) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                    (
                        run_id,
                        value["night_date"],
                        checked_at,
                        value["policy_version"],
                        value["data_state"],
                        int(value["core_ready"]),
                        value["source_fingerprint"],
                        encoded,
                        digest,
                    ),
                )
                inserted += 1
                latest_id = connection.execute(
                    """
                    SELECT check_id FROM analysis_readiness_checks WHERE night_date=?
                    ORDER BY checked_at DESC,check_id DESC LIMIT 1
                """,
                    (value["night_date"],),
                ).fetchone()[0]
                if latest_id == cursor.lastrowid:
                    connection.execute(
                        """
                        INSERT INTO analysis_readiness VALUES (?,?)
                        ON CONFLICT(night_date) DO UPDATE SET check_id=excluded.check_id
                    """,
                        (value["night_date"], latest_id),
                    )
                    updated += 1
        secure_database_files(data_dir)
        return {
            "status": "saved",
            "inserted": inserted,
            "current_updated": updated,
            "duplicates": duplicates,
            "result_count": len(results),
        }
    except AnalysisStorageError:
        raise
    except CapacityError:
        _fail("analysis_capacity_blocked")
    except Exception as exc:
        _fail("analysis_storage_full" if is_disk_full(exc) else "analysis_write_failed")
