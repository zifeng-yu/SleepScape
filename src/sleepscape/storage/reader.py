"""Read-only dashboard queries and run metadata; never initializes or reads raw payloads."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SUCCESS = {"ok", "partial", "empty"}
SUMMARY_KINDS = (
    "sleep",
    "hrv_daily",
    "daily_summary",
    "daily_summary_legacy",
    "stress_daily",
    "steps_daily",
    "activity",
    "nap",
)
SAMPLE_KINDS = ("heart_rate_sample", "hrv_sample", "spo2_sample", "body_battery_sample")
_RECORD_COLUMNS = "resource,kind,record_key,date,timestamp,data_json,fetched_at"


class StorageReadError(Exception):
    """A storage read failed; expose no database path or SQLite error text."""


def completed_run_plans(data_dir: Path) -> list[dict]:
    """Read sync planning inputs without initializing or mutating a database."""
    try:
        with DashboardReader(data_dir) as reader:
            if reader.path.exists() and not reader.present:
                raise StorageReadError("run_metadata_unavailable")
            rows = reader._query("SELECT plan_json FROM runs WHERE status='completed'")
            plans = [json.loads(row[0]) for row in rows]
            if any(not isinstance(plan, dict) for plan in plans):
                raise StorageReadError("run_metadata_unavailable")
            return plans
    except (sqlite3.Error, OSError, ValueError, TypeError):
        raise StorageReadError("run_metadata_unavailable") from None


def validate_dates(start: date, end: date, maximum: int = 123) -> None:
    if start > end or (end - start).days >= maximum:
        raise ValueError("invalid_date_range")


def validate_window(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("timezone_required")
    if start >= end or end.astimezone(UTC) - start.astimezone(UTC) > timedelta(hours=48):
        raise ValueError("invalid_time_window")


def _records(rows) -> list[dict]:
    result = []
    for row in rows:
        item = dict(row)
        item["key"] = item.pop("record_key")
        item["data"] = json.loads(item.pop("data_json"))
        result.append(item)
    return result


def _attempt(row) -> dict:
    item = dict(row)
    item["metrics"] = json.loads(item.pop("metrics_json"))
    item["issues"] = json.loads(item.pop("issues_json"))
    return item


class DashboardReader:
    """One coherent read transaction per service call, including a missing database."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "sleepscape.sqlite3"
        self.connection: sqlite3.Connection | None = None

    def __enter__(self):
        if self.path.is_file():
            self.connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            self.connection.row_factory = sqlite3.Row
            try:
                self.connection.execute("PRAGMA query_only = ON")
                self.connection.execute("BEGIN")
            except Exception:
                self.close()
                raise
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _query(self, sql: str, values=()):
        return self.connection.execute(sql, values).fetchall() if self.connection else []

    @property
    def present(self) -> bool:
        return self.connection is not None

    def summaries(self, start: date, end: date) -> list[dict]:
        # Public history is at most 92 days; the first displayed day needs 31 older days.
        validate_dates(start, end)
        placeholders = ",".join("?" for _ in SUMMARY_KINDS)
        return _records(
            self._query(
                f"SELECT {_RECORD_COLUMNS} FROM normalized_records "
                f"WHERE date BETWEEN ? AND ? AND kind IN ({placeholders}) ORDER BY date,fetched_at",
                (start.isoformat(), end.isoformat(), *SUMMARY_KINDS),
            )
        )

    def attempts(self, start: date, end: date) -> list[dict]:
        validate_dates(start, end)
        # Keep the last attempt and last successful attempt for every resource/date.
        # Bounds can overlap differently, so callers resolve per day instead of key.
        rows = self._query(
            "SELECT id,resource,start_date,end_date,fetched_at,status,error_code,"
            "metrics_json,issues_json FROM requests WHERE start_date <= ? AND end_date >= ? "
            "ORDER BY id",
            (end.isoformat(), start.isoformat()),
        )
        return [_attempt(row) for row in rows]

    def samples(self, start: datetime, end: datetime) -> list[dict]:
        validate_window(start, end)
        placeholders = ",".join("?" for _ in SAMPLE_KINDS)
        # julianday handles equivalent UTC offsets and fractional seconds correctly.
        return _records(
            self._query(
                f"SELECT {_RECORD_COLUMNS} FROM normalized_records "
                f"WHERE kind IN ({placeholders}) AND timestamp >= ? AND timestamp < ? "
                "AND julianday(timestamp) BETWEEN "
                "julianday(?) AND julianday(?) ORDER BY julianday(timestamp),resource,record_key",
                (
                    *SAMPLE_KINDS,
                    (start.astimezone(UTC) - timedelta(days=1)).date().isoformat(),
                    (end.astimezone(UTC) + timedelta(days=2)).date().isoformat(),
                    start.astimezone(UTC).isoformat(),
                    end.astimezone(UTC).isoformat(),
                ),
            )
        )

    def events(self, start: datetime, end: datetime) -> list[dict]:
        validate_window(start, end)
        return _records(
            self._query(
                f"SELECT {_RECORD_COLUMNS} FROM normalized_records "
                "WHERE kind IN ('activity','nap','sleep_stage') "
                "AND julianday(json_extract(data_json,'$.start_at')) <= julianday(?) "
                "AND julianday(COALESCE(json_extract(data_json,'$.end_at'),"
                "json_extract(data_json,'$.start_at'))) >= julianday(?) "
                "ORDER BY julianday(json_extract(data_json,'$.start_at')),resource,record_key",
                (end.astimezone(UTC).isoformat(), start.astimezone(UTC).isoformat()),
            )
        )

    def available_dates(self, start: date, end: date) -> list[str]:
        validate_dates(start, end, 92)
        return [
            row[0]
            for row in self._query(
                "SELECT DISTINCT date FROM normalized_records WHERE kind='sleep' "
                "AND resource IN ('sleep','sleep_summaries') AND date BETWEEN ? AND ? "
                "ORDER BY date DESC",
                (start.isoformat(), end.isoformat()),
            )
        ]

    def status(self) -> dict:
        counts = {"record_count": 0, "raw_snapshot_count": 0, "request_count": 0}
        for key, table in (
            ("record_count", "normalized_records"),
            ("raw_snapshot_count", "raw_snapshots"),
            ("request_count", "requests"),
        ):
            rows = self._query(f"SELECT COUNT(*) FROM {table}")
            counts[key] = rows[0][0] if rows else 0
        nights = self._query(
            "SELECT MIN(date),MAX(date) FROM normalized_records WHERE kind='sleep' "
            "AND resource IN ('sleep','sleep_summaries')"
        )
        runs = self._query(
            "SELECT status,started_at,finished_at,plan_json,summary_json FROM runs "
            "ORDER BY started_at DESC,rowid DESC LIMIT 1"
        )
        run = dict(runs[0]) if runs else None
        if run:
            run["plan"] = json.loads(run.pop("plan_json"))
            run["summary"] = json.loads(run.pop("summary_json") or "{}")
        latest = self._query(
            "SELECT r.id,r.resource,r.start_date,r.end_date,r.fetched_at,r.status,r.error_code,"
            "r.metrics_json,r.issues_json FROM requests r JOIN "
            "(SELECT resource,MAX(id) AS last_id FROM requests GROUP BY resource) x "
            "ON r.id=x.last_id ORDER BY r.resource"
        )
        successes = dict(
            self._query(
                "SELECT resource,MAX(fetched_at) FROM requests "
                "WHERE status IN ('ok','partial','empty') "
                "GROUP BY resource"
            )
        )
        size = sum(
            path.stat().st_size
            for path in (self.path, Path(str(self.path) + "-wal"))
            if path.is_file()
        )
        return {
            "present": self.present,
            "size_bytes": size,
            **counts,
            "earliest_night": nights[0][0] if nights else None,
            "latest_night": nights[0][1] if nights else None,
            "latest_run": run,
            "resources": [_attempt(row) for row in latest],
            "last_success": successes,
            "data_as_of": max(successes.values(), default=None),
        }

    def data_as_of(self) -> str | None:
        rows = self._query(
            "SELECT MAX(fetched_at) FROM requests WHERE status IN ('ok','partial','empty')"
        )
        return rows[0][0] if rows else None
