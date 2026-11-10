"""Small SQLite repository; no network access or credential handling."""

import hashlib
import json
import os
import sqlite3
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sleepscape.domain.models import DataRequest, FetchResult, JsonValue, NormalizedBatch

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS raw_snapshots (
    id INTEGER PRIMARY KEY,
    resource TEXT NOT NULL,
    request_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_zlib BLOB NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE (resource, request_key, content_hash)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    resource TEXT NOT NULL,
    request_key TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    fetched_at TEXT NOT NULL,
    http_status INTEGER,
    status TEXT NOT NULL,
    error_code TEXT,
    snapshot_id INTEGER REFERENCES raw_snapshots(id),
    record_count INTEGER NOT NULL DEFAULT 0,
    issues_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_run ON requests(run_id, id);
CREATE INDEX IF NOT EXISTS requests_resource ON requests(resource, request_key, id);
CREATE TABLE IF NOT EXISTS normalized_records (
    resource TEXT NOT NULL,
    kind TEXT NOT NULL,
    record_key TEXT NOT NULL,
    date TEXT,
    timestamp TEXT,
    data_json TEXT NOT NULL,
    request_key TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES raw_snapshots(id),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (resource, kind, record_key)
);
CREATE INDEX IF NOT EXISTS records_scope ON normalized_records(resource, date);
CREATE INDEX IF NOT EXISTS records_request ON normalized_records(resource, request_key);
CREATE INDEX IF NOT EXISTS records_instant ON normalized_records(kind, julianday(timestamp));
CREATE INDEX IF NOT EXISTS records_kind_date ON normalized_records(kind, date);
CREATE INDEX IF NOT EXISTS records_event_start ON normalized_records
    (kind, julianday(json_extract(data_json, '$.start_at')))
    WHERE kind IN ('activity', 'nap', 'sleep_stage');
CREATE INDEX IF NOT EXISTS requests_scope ON requests(resource, start_date, end_date, id);
"""


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    """One connection per repository; transactions keep snapshots and current rows consistent."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        self.path = self.data_dir / "sleepscape.sqlite3"
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        self._connection = sqlite3.connect(self.path, timeout=30)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(_SCHEMA)
            self._secure_files()
        except Exception:
            self._connection.close()
            raise

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _secure_files(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                path.chmod(0o600)

    def start_run(self, plan: dict) -> str:
        run_id = uuid4().hex
        with self._connection:
            self._connection.execute(
                "INSERT INTO runs (id, started_at, status, plan_json) VALUES (?, ?, ?, ?)",
                (run_id, _now(), "running", _json(plan)),
            )
        self._secure_files()
        return run_id

    def save_snapshot(self, run_id: str, result: FetchResult, batch: NormalizedBatch) -> dict:
        request = result.request
        payload = _json(result.payload).encode("utf-8")
        content_hash = hashlib.sha256(payload).hexdigest()
        valid = batch.metrics.get("payload_shape_valid", True) is not False
        issues = [
            {key: issue[key] for key in ("severity", "code", "field") if key in issue}
            for issue in batch.issues
        ]
        if not valid:
            status = "invalid"
        elif batch.metrics.get("raw_only") and batch.metrics.get("item_count", 0) > 0:
            status = "partial" if issues else "ok"
        elif not batch.records:
            status = "empty"
        else:
            status = "partial" if issues else "ok"
        records = batch.records if valid else []
        with self._connection:
            cursor = self._connection.execute(
                """INSERT INTO raw_snapshots
                   (resource, request_key, content_hash, payload_zlib, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                (
                    request.resource,
                    request.key,
                    content_hash,
                    zlib.compress(payload),
                    result.fetched_at,
                    result.fetched_at,
                ),
            )
            new_snapshot = cursor.rowcount == 1
            snapshot_id = self._connection.execute(
                """SELECT id FROM raw_snapshots
                   WHERE resource = ? AND request_key = ? AND content_hash = ?""",
                (request.resource, request.key, content_hash),
            ).fetchone()["id"]
            self._connection.execute(
                "UPDATE raw_snapshots SET last_seen = ? WHERE id = ?",
                (result.fetched_at, snapshot_id),
            )
            if valid:
                self._replace_records(request, records, snapshot_id, result.fetched_at)
            self._insert_request(
                run_id,
                request,
                result.fetched_at,
                result.http_status,
                status,
                snapshot_id=snapshot_id,
                record_count=len(records),
                issues=issues,
                metrics=batch.metrics,
            )
        self._secure_files()
        return {
            "snapshot_id": snapshot_id,
            "new_snapshot": new_snapshot,
            "record_count": len(records),
            "status": status,
        }

    def _replace_records(
        self, request: DataRequest, records: list[dict], snapshot_id: int, fetched_at: str
    ) -> None:
        parameters: list[Any] = [request.resource]
        if request.start_date is not None or request.end_date is not None:
            date_conditions = ["date IS NOT NULL"]
            if request.start_date is not None:
                date_conditions.append("date >= ?")
                parameters.append(request.start_date)
            if request.end_date is not None:
                date_conditions.append("date <= ?")
                parameters.append(request.end_date)
            scope = " AND ".join(date_conditions)
            # date is the source endpoint's partition day, not the UTC sample day.
            # Clear the same request too, including corrections to older day assignments.
            scope = f"(({scope}) OR request_key = ?)"
            parameters.append(request.key)
        else:
            scope = "request_key = ?"
            parameters.append(request.key)
        self._connection.execute(
            f"DELETE FROM normalized_records WHERE resource = ? AND {scope}", parameters
        )
        for record in records:
            self._connection.execute(
                """INSERT INTO normalized_records
                   (resource, kind, record_key, date, timestamp, data_json,
                    request_key, snapshot_id, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (resource, kind, record_key) DO UPDATE SET
                     date=excluded.date, timestamp=excluded.timestamp, data_json=excluded.data_json,
                     request_key=excluded.request_key, snapshot_id=excluded.snapshot_id,
                     fetched_at=excluded.fetched_at""",
                (
                    request.resource,
                    record["kind"],
                    record["key"],
                    record.get("date"),
                    record.get("timestamp"),
                    _json(record["data"]),
                    request.key,
                    snapshot_id,
                    fetched_at,
                ),
            )

    def _insert_request(
        self,
        run_id: str,
        request: DataRequest,
        fetched_at: str,
        http_status: int | None,
        status: str,
        *,
        error_code: str | None = None,
        snapshot_id: int | None = None,
        record_count: int = 0,
        issues: list | None = None,
        metrics: dict | None = None,
    ) -> None:
        self._connection.execute(
            """INSERT INTO requests
               (run_id, resource, request_key, start_date, end_date, fetched_at, http_status,
                status, error_code, snapshot_id, record_count, issues_json, metrics_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                request.resource,
                request.key,
                request.start_date,
                request.end_date,
                fetched_at,
                http_status,
                status,
                error_code,
                snapshot_id,
                record_count,
                _json(issues or []),
                _json(metrics or {}),
            ),
        )

    def record_failure(
        self,
        run_id: str,
        request: DataRequest,
        error_code: str,
        http_status: int | None = None,
    ) -> None:
        with self._connection:
            self._insert_request(
                run_id, request, _now(), http_status, "failed", error_code=error_code
            )
        self._secure_files()

    def finish_run(self, run_id: str, status: str, summary: dict) -> None:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE runs SET finished_at = ?, status = ?, summary_json = ? WHERE id = ?",
                (_now(), status, _json(summary), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown collection run")
        self._secure_files()

    def latest_run(self) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        run = dict(row)
        run["plan"] = json.loads(run.pop("plan_json"))
        summary = run.pop("summary_json")
        run["summary"] = json.loads(summary) if summary is not None else None
        return run

    def list_records(
        self,
        kind: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        resource: str | None = None,
    ) -> list[dict]:
        conditions, parameters = [], []
        for clause, value in (
            ("kind = ?", kind),
            ("date >= ?", start_date),
            ("date <= ?", end_date),
            ("resource = ?", resource),
        ):
            if value is not None:
                conditions.append(clause)
                parameters.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self._connection.execute(
            "SELECT resource, kind, record_key AS key, date, timestamp, data_json, "
            "request_key, snapshot_id, fetched_at FROM normalized_records"
            + where
            + " ORDER BY date, timestamp, resource, kind, record_key",
            parameters,
        )
        records = []
        for row in rows:
            record = dict(row)
            record["data"] = json.loads(record.pop("data_json"))
            records.append(record)
        return records

    def list_requests(self, run_id: str | None = None) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM requests" + (" WHERE run_id = ?" if run_id else "") + " ORDER BY id",
            (run_id,) if run_id else (),
        )
        requests = []
        for row in rows:
            request = dict(row)
            request["issues"] = json.loads(request.pop("issues_json"))
            request["metrics"] = json.loads(request.pop("metrics_json"))
            requests.append(request)
        return requests

    def counts(self) -> dict:
        return {
            table: self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "raw_snapshots", "normalized_records", "requests")
        }

    def get_raw(self, resource: str, key: str) -> JsonValue:
        row = self._connection.execute(
            """SELECT raw.payload_zlib FROM requests AS req
               JOIN raw_snapshots AS raw ON raw.id = req.snapshot_id
               WHERE req.resource = ? AND req.request_key = ?
                 AND req.status IN ('ok', 'partial', 'empty')
               ORDER BY req.fetched_at DESC, req.id DESC LIMIT 1""",
            (resource, key),
        ).fetchone()
        return json.loads(zlib.decompress(row[0])) if row else None

    def integrity_check(self) -> list[str]:
        return [row[0] for row in self._connection.execute("PRAGMA integrity_check")]
