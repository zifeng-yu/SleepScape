"""Private immutable inputs/reports and durable physical-request attempts.

Only explicit calls migrate/write. Services own source rechecks and model budgets.
"""

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from sleepscape.models.base import OUTPUT_SCHEMA_ERRORS, TRANSPORT_NORMALIZATIONS
from sleepscape.storage.repository import secure_database_files
from sleepscape.storage.usage import CapacityError, is_disk_full, require_capacity

REPORT_VERSION = 1
REPORT_TABLES = (
    "sleep_analysis_schema",
    "analysis_inputs",
    "analysis_attempts",
    "sleep_analysis_reports",
)
MAX_INPUT_BYTES = 24 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
STATES = frozenset(
    {
        "running",
        "complete",
        "failed",
        "discarded_input_changed",
        "discarded_already_published",
        "abandoned",
    }
)
ERROR_CODES = (
    frozenset(
        {
            "model_not_configured",
            "model_configuration_invalid",
            "model_key_missing",
            "model_key_permissions",
            "model_key_invalid",
            "model_disabled",
            "invalid_model_request",
            "capability_unavailable",
            "authentication_failed",
            "permission_denied",
            "model_unavailable",
            "rate_limited",
            "service_unavailable",
            "http_error",
            "network_error",
            "request_timeout",
            "response_too_large",
            "invalid_response",
            "invalid_json",
            "output_schema_mismatch",
            "output_truncated",
            "model_mismatch",
            "model_operation_failed",
            "model_busy",
            "budget_exhausted",
            "model_check_failed",
            "model_state_unavailable",
            "model_deadline_exceeded",
            "model_worker_failed",
            "storage_capacity_critical",
            "storage_capacity_restricted",
            "storage_capacity_unavailable",
            "analysis_output_invalid",
            "analysis_input_invalid",
            "input_changed",
            "already_published",
            "analysis_interrupted",
            "invalid_analysis_input",
            "invalid_analysis_output",
            "model_insufficient_input",
            "output_business_mismatch",
            "analysis_not_ready",
            "blocked_input_budget",
        }
    )
    | OUTPUT_SCHEMA_ERRORS
)
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_BASE = {"runs", "raw_snapshots", "normalized_records", "requests"}
_DDL = {
    "sleep_analysis_schema": """CREATE TABLE sleep_analysis_schema (
        version INTEGER PRIMARY KEY, created_at TEXT NOT NULL)""",
    "analysis_inputs": """CREATE TABLE analysis_inputs (
        input_id TEXT PRIMARY KEY, night_date TEXT NOT NULL, input_hash TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL, prompt_hash TEXT NOT NULL, schema_hash TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        model_ref TEXT NOT NULL, config_fingerprint TEXT NOT NULL,
        time_mapping_version TEXT NOT NULL,
        created_at TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        UNIQUE(night_date,input_hash,source_fingerprint,prompt_hash,schema_hash,config_fingerprint),
        UNIQUE(input_id,night_date))""",
    "analysis_attempts": """CREATE TABLE analysis_attempts (
        attempt_id TEXT PRIMARY KEY, input_id TEXT NOT NULL, night_date TEXT NOT NULL,
        request_id TEXT NOT NULL UNIQUE, started_at TEXT NOT NULL, finished_at TEXT,
        state TEXT NOT NULL CHECK(state IN ('running','complete','failed','discarded_input_changed',
            'discarded_already_published','abandoned')),
        error_code TEXT, retry_after INTEGER,
        FOREIGN KEY(input_id,night_date) REFERENCES analysis_inputs(input_id,night_date),
        UNIQUE(attempt_id,night_date))""",
    "sleep_analysis_reports": """CREATE TABLE sleep_analysis_reports (
        night_date TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE, generated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        model_json TEXT NOT NULL, model_hash TEXT NOT NULL,
        FOREIGN KEY(attempt_id,night_date) REFERENCES analysis_attempts(attempt_id,night_date))""",
    "analysis_attempts_night_started": """CREATE INDEX analysis_attempts_night_started
        ON analysis_attempts(night_date,started_at,attempt_id)""",
    "analysis_attempts_started": """CREATE INDEX analysis_attempts_started
        ON analysis_attempts(started_at,attempt_id)""",
}
for _table in ("analysis_inputs", "sleep_analysis_reports"):
    for _op in ("update", "delete"):
        _DDL[f"{_table}_no_{_op}"] = f"""CREATE TRIGGER {_table}_no_{_op}
            BEFORE {_op.upper()} ON {_table}
            BEGIN SELECT RAISE(ABORT,'sleep_analysis_immutable'); END"""
_DDL["analysis_attempts_no_delete"] = """CREATE TRIGGER analysis_attempts_no_delete
    BEFORE DELETE ON analysis_attempts
    BEGIN SELECT RAISE(ABORT,'sleep_analysis_immutable'); END"""
_DDL["analysis_attempts_terminal_guard"] = """CREATE TRIGGER analysis_attempts_terminal_guard
    BEFORE UPDATE ON analysis_attempts
    WHEN OLD.state != 'running' OR NEW.state = 'running'
      OR NEW.attempt_id != OLD.attempt_id OR NEW.input_id != OLD.input_id
      OR NEW.night_date != OLD.night_date OR NEW.request_id != OLD.request_id
      OR NEW.started_at != OLD.started_at
    BEGIN SELECT RAISE(ABORT,'sleep_analysis_attempt_immutable'); END"""


REVISION_TABLE = "sleep_analysis_revisions"
_REVISION_DDL = {
    REVISION_TABLE: """CREATE TABLE sleep_analysis_revisions (
        night_date TEXT NOT NULL REFERENCES sleep_analysis_reports(night_date),
        attempt_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        model_json TEXT NOT NULL, model_hash TEXT NOT NULL,
        FOREIGN KEY(attempt_id,night_date) REFERENCES analysis_attempts(attempt_id,night_date))""",
    "sleep_analysis_revisions_night": """CREATE INDEX sleep_analysis_revisions_night
        ON sleep_analysis_revisions(night_date)""",
}
for _op in ("update", "delete"):
    _name = f"sleep_analysis_revisions_no_{_op}"
    _REVISION_DDL[_name] = f"""CREATE TRIGGER {_name}
        BEFORE {_op.upper()} ON sleep_analysis_revisions
        BEGIN SELECT RAISE(ABORT,'sleep_analysis_immutable'); END"""


def revisions_available(c):
    objects = dict(
        c.execute(
            "SELECT name,sql FROM sqlite_master WHERE name IN (?,?,?,?)", tuple(_REVISION_DDL)
        )
    )
    if objects and objects != _REVISION_DDL:
        _fail("sleep_analysis_schema_invalid")
    return bool(objects)


class SleepAnalysisStorageError(Exception):
    """Stable error codes, never private SQLite messages or payloads."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)
        self.__suppress_context__ = True


def _fail(code="sleep_analysis_payload_invalid"):
    raise SleepAnalysisStorageError(code)


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail()
    return value


def _day(value):
    if type(value) is date:
        value = value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail()
    try:
        year = date.fromisoformat(value).year
    except ValueError:
        _fail()
    if not 2000 <= year <= 2100:
        _fail()
    return value


def _instant(value=None):
    value = datetime.now(UTC) if value is None else value
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            _fail()
    if not isinstance(value, datetime) or value.tzinfo is None or not 2000 <= value.year <= 2100:
        _fail()
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _input_payload(payload):
    from sleepscape.resources import validate_sleep_input

    try:
        validate_sleep_input(payload)
        raw = _json(payload)
        if len(raw.encode()) > MAX_INPUT_BYTES:
            _fail()
        return raw
    except SleepAnalysisStorageError:
        raise
    except Exception:
        _fail("sleep_analysis_input_invalid")


def _output_payload(payload, source):
    from sleepscape.resources import validate_sleep_output

    try:
        validate_sleep_output(payload, source)
        if payload["status"] != "complete":
            _fail()
        raw = _json(payload)
        if len(raw.encode()) > MAX_OUTPUT_BYTES:
            _fail()
        return raw
    except SleepAnalysisStorageError:
        raise
    except Exception:
        _fail("sleep_analysis_output_invalid")


def _model_metadata(value):
    """Long-term safe result metadata, independent of the short-lived runtime ledger."""
    if value == {}:
        return "{}"
    if not isinstance(value, dict) or set(value) - {"transport_normalization"} != {
        "model",
        "usage",
        "duration_ms",
        "finish_reason",
    }:
        _fail("sleep_analysis_model_metadata_invalid")
    if (
        not isinstance(value["model"], str)
        or not re.fullmatch(r"[a-zA-Z0-9_./:-]{1,128}", value["model"])
        or "://" in value["model"]
        or value["finish_reason"] != "stop"
        or type(value["duration_ms"]) is not int
        or not 0 <= value["duration_ms"] <= 600000
        or (
            "transport_normalization" in value
            and (
                not isinstance(value["transport_normalization"], str)
                or value["transport_normalization"] not in TRANSPORT_NORMALIZATIONS - {"none"}
            )
        )
    ):
        _fail("sleep_analysis_model_metadata_invalid")
    usage = value["usage"]
    if (
        not isinstance(usage, dict)
        or not set(usage) <= {"prompt_tokens", "completion_tokens", "total_tokens"}
        or any(type(n) is not int or not 0 <= n <= 2**53 - 1 for n in usage.values())
    ):
        _fail("sleep_analysis_model_metadata_invalid")
    return _json(value)


def _row(cursor):
    value = cursor.fetchone()
    return dict(zip((c[0] for c in cursor.description), value, strict=True)) if value else None


def report_available(connection):
    if connection is None:
        return False
    try:
        slots = ",".join("?" for _ in _DDL)
        objects = dict(
            connection.execute(
                f"SELECT name,sql FROM sqlite_master WHERE name IN ({slots})", tuple(_DDL)
            )
        )
        if not objects:
            return False
        if set(objects) != set(_DDL):
            _fail("sleep_analysis_schema_incomplete")

        def normalize(text):
            return " ".join(text.lower().split())

        if any(not objects[n] or normalize(objects[n]) != normalize(s) for n, s in _DDL.items()):
            _fail("sleep_analysis_schema_invalid")
        rows = connection.execute("SELECT version,created_at FROM sleep_analysis_schema").fetchall()
        if len(rows) != 1 or rows[0][0] != REPORT_VERSION:
            _fail("sleep_analysis_schema_unsupported")
        _instant(rows[0][1])
        return True
    except SleepAnalysisStorageError:
        raise
    except Exception:
        _fail("sleep_analysis_schema_invalid")


@contextmanager
def _database(data_dir, *, writable=False):
    path = Path(data_dir) / "sleepscape.sqlite3"
    connection = None
    try:
        if path.is_symlink() or path.parent.is_symlink():
            _fail("sleep_analysis_source_unavailable")
        if not path.exists() and not writable:
            yield None
            return
        if not path.is_file():
            _fail("sleep_analysis_source_unavailable")
        mode = "rw" if writable else "ro"
        connection = sqlite3.connect(path.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        if not writable:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not _BASE <= tables:
            _fail("sleep_analysis_base_schema_invalid")
        yield connection
    finally:
        if connection is not None:
            connection.close()


@contextmanager
def _write(data_dir, required_bytes=0):
    try:
        require_capacity(Path(data_dir), "write", max(4096, required_bytes) * 2)
        with _database(data_dir, writable=True) as c, c:
            c.execute("BEGIN IMMEDIATE")
            if not report_available(c):
                _fail("sleep_analysis_not_initialized")
            yield c
        secure_database_files(data_dir)
    except SleepAnalysisStorageError:
        raise
    except CapacityError:
        _fail("sleep_analysis_capacity_blocked")
    except Exception as exc:
        _fail("sleep_analysis_storage_full" if is_disk_full(exc) else "sleep_analysis_write_failed")


def migrate_reports(data_dir):
    """Explicit setup; the service must verify a backup first."""
    try:
        with _database(data_dir, writable=True) as c, c:
            if report_available(c):
                return {"status": "already_initialized", "schema_version": REPORT_VERSION}
            require_capacity(Path(data_dir), "write", 256 * 1024)
            c.execute("BEGIN IMMEDIATE")
            if report_available(c):
                return {"status": "already_initialized", "schema_version": REPORT_VERSION}
            for sql in _DDL.values():
                c.execute(sql)
            c.execute(
                "INSERT INTO sleep_analysis_schema VALUES (?,?)", (REPORT_VERSION, _instant())
            )
            report_available(c)
        secure_database_files(data_dir)
        return {"status": "initialized", "schema_version": REPORT_VERSION}
    except SleepAnalysisStorageError:
        raise
    except CapacityError:
        _fail("sleep_analysis_capacity_blocked")
    except Exception as exc:
        _fail(
            "sleep_analysis_storage_full"
            if is_disk_full(exc)
            else "sleep_analysis_migration_failed"
        )


def _decode_input(row):
    try:
        _check(row["input_id"], _ID)
        _day(row["night_date"])
        for name in (
            "input_hash",
            "source_fingerprint",
            "prompt_hash",
            "schema_hash",
            "config_fingerprint",
            "payload_hash",
        ):
            _check(row[name], _HASH)
        for name in ("prompt_version", "model_ref", "time_mapping_version"):
            _check(row[name], _CODE)
        created = _instant(row["created_at"])
        if created != row["created_at"]:
            _fail()
        raw = row["payload_json"]
        if not isinstance(raw, str) or len(raw.encode()) > MAX_INPUT_BYTES:
            _fail()
        payload = json.loads(raw)
        if _input_payload(payload) != raw or _hash(raw) != row["payload_hash"]:
            _fail("sleep_analysis_payload_hash_invalid")
        if payload["night_date"] != row["night_date"] or payload["input_hash"] != row["input_hash"]:
            _fail("sleep_analysis_projection_invalid")
        if _instant(payload["data_cutoff_at"]) > created:
            _fail("sleep_analysis_projection_invalid")
        return {k: v for k, v in row.items() if k != "payload_json"} | {"payload": payload}
    except SleepAnalysisStorageError:
        raise
    except Exception:
        _fail("sleep_analysis_payload_invalid")


def _input(c, input_id):
    row = _row(
        c.execute("SELECT * FROM analysis_inputs WHERE input_id=?", (_check(input_id, _ID),))
    )
    return _decode_input(row) if row else None


def _decode_attempt(row, source):
    if source is None:
        _fail("sleep_analysis_reference_invalid")
    try:
        for key in ("attempt_id", "request_id", "input_id"):
            _check(row[key], _ID)
        if row["input_id"] != source["input_id"] or row["night_date"] != source["night_date"]:
            _fail("sleep_analysis_reference_invalid")
        start = _instant(row["started_at"])
        if start != row["started_at"] or start < source["created_at"] or row["state"] not in STATES:
            _fail()
        if row["state"] == "running":
            if any(row[k] is not None for k in ("finished_at", "error_code", "retry_after")):
                _fail()
        else:
            if _instant(row["finished_at"]) != row["finished_at"] or row["finished_at"] < start:
                _fail()
            if row["state"] == "complete":
                if row["error_code"] is not None or row["retry_after"] is not None:
                    _fail()
            elif row["error_code"] not in ERROR_CODES:
                _fail()
            if row["retry_after"] is not None and (
                type(row["retry_after"]) is not int or not 0 <= row["retry_after"] <= 86400
            ):
                _fail()
        return row | {"input_hash": source["input_hash"]}
    except SleepAnalysisStorageError:
        raise
    except Exception:
        _fail("sleep_analysis_attempt_invalid")


def _attempt(c, attempt_id):
    row = _row(
        c.execute("SELECT * FROM analysis_attempts WHERE attempt_id=?", (_check(attempt_id, _ID),))
    )
    if row is None:
        return None
    result = _decode_attempt(row, _input(c, row["input_id"]))
    if result["state"] == "complete":
        report = c.execute(
            "SELECT attempt_id FROM sleep_analysis_reports WHERE night_date=?",
            (result["night_date"],),
        ).fetchone()
        matches = int(report is not None and report[0] == attempt_id)
        if revisions_available(c):
            matches += c.execute(
                "SELECT count(*) FROM sleep_analysis_revisions WHERE attempt_id=? AND night_date=?",
                (attempt_id, result["night_date"]),
            ).fetchone()[0]
        if matches != 1:
            _fail("sleep_analysis_reference_invalid")
    return result


def _report(c, night_date):
    row = _row(
        c.execute("SELECT * FROM sleep_analysis_reports WHERE night_date=?", (_day(night_date),))
    )
    if row is not None and revisions_available(c):
        revision = _row(
            c.execute(
                "SELECT * FROM sleep_analysis_revisions WHERE night_date=? "
                "ORDER BY rowid DESC LIMIT 1",
                (night_date,),
            )
        )
        row = revision or row
    return _decode_report(c, row, night_date)


def _decode_report(c, row, night_date):
    if row is None:
        if c.execute(
            "SELECT 1 FROM analysis_attempts WHERE night_date=? AND state='complete' LIMIT 1",
            (_day(night_date),),
        ).fetchone():
            _fail("sleep_analysis_reference_invalid")
        return None
    try:
        attempt = _attempt(c, row["attempt_id"])
        if (
            attempt is None
            or attempt["state"] != "complete"
            or attempt["night_date"] != row["night_date"]
        ):
            _fail("sleep_analysis_reference_invalid")
        if row["generated_at"] != attempt["finished_at"]:
            _fail("sleep_analysis_projection_invalid")
        source = _input(c, attempt["input_id"])
        raw = row["payload_json"]
        if not isinstance(raw, str) or len(raw.encode()) > MAX_OUTPUT_BYTES:
            _fail()
        output = json.loads(raw)
        if _output_payload(output, source["payload"]) != raw or _hash(raw) != row["payload_hash"]:
            _fail("sleep_analysis_payload_hash_invalid")
        if not isinstance(row["model_json"], str) or len(row["model_json"].encode()) > 4096:
            _fail("sleep_analysis_model_metadata_invalid")
        metadata = json.loads(row["model_json"])
        if (
            _model_metadata(metadata) != row["model_json"]
            or _hash(row["model_json"]) != row["model_hash"]
        ):
            _fail("sleep_analysis_payload_hash_invalid")
        return {k: v for k, v in row.items() if k not in {"payload_json", "model_json"}} | {
            "output": output,
            "input": source,
            "attempt": attempt,
            "model_metadata": metadata,
        }
    except SleepAnalysisStorageError:
        raise
    except Exception:
        _fail("sleep_analysis_report_invalid")


def _read(data_dir, connection, fn):
    try:
        if connection is not None:
            return fn(connection) if report_available(connection) else None
        with _database(data_dir) as c:
            return fn(c) if report_available(c) else None
    except SleepAnalysisStorageError:
        raise
    except Exception:
        _fail("sleep_analysis_read_failed")


def read_input(data_dir, input_id, *, connection=None):
    return _read(data_dir, connection, lambda c: _input(c, input_id))


def read_report(data_dir, night_date, *, connection=None):
    return _read(data_dir, connection, lambda c: _report(c, night_date))


def read_report_summaries(data_dir, days, *, connection=None):
    """At most 92 dates; no source recomputation or input/answer content in results."""

    def query(c):
        values = list(days)
        if len(values) > 92:
            _fail("sleep_analysis_date_range_invalid")
        result = {}
        for day in dict.fromkeys(_day(value) for value in values):
            saved = _report(c, day)
            if saved is not None:
                result[day] = {"state": "complete", "generated_at": saved["generated_at"]}
        return result

    return _read(data_dir, connection, query) or {}


def read_attempts(data_dir, night_date, *, input_hash=None, connection=None):
    def query(c):
        ids = c.execute(
            "SELECT a.attempt_id FROM analysis_attempts a "
            "JOIN analysis_inputs i ON i.input_id=a.input_id "
            "WHERE a.night_date=?"
            + (" AND i.input_hash=?" if input_hash is not None else "")
            + " ORDER BY a.started_at,a.attempt_id",
            (_day(night_date), _check(input_hash, _HASH))
            if input_hash is not None
            else (_day(night_date),),
        ).fetchall()
        return [_attempt(c, row[0]) for row in ids]

    return _read(data_dir, connection, query) or []


def read_attempted_nights(data_dir, day, *, connection=None):
    """All reserved nights on a Shanghai day, independent of the rolling window."""

    def query(c):
        start = datetime.combine(date.fromisoformat(_day(day)), time.min, ZoneInfo("Asia/Shanghai"))
        ids = c.execute(
            "SELECT attempt_id FROM analysis_attempts WHERE started_at>=? AND started_at<? "
            "ORDER BY started_at,attempt_id",
            (_instant(start), _instant(start + timedelta(days=1))),
        ).fetchall()
        return sorted({_attempt(c, row[0])["night_date"] for row in ids})

    return _read(data_dir, connection, query) or []


def save_input(
    data_dir,
    payload,
    *,
    source_fingerprint,
    prompt_hash,
    schema_hash,
    prompt_version,
    model_ref,
    config_fingerprint,
    time_mapping_version,
    now=None,
):
    raw = _input_payload(payload)
    row = {
        "input_id": uuid4().hex,
        "night_date": payload["night_date"],
        "input_hash": payload["input_hash"],
        "source_fingerprint": source_fingerprint,
        "prompt_hash": prompt_hash,
        "schema_hash": schema_hash,
        "prompt_version": prompt_version,
        "model_ref": model_ref,
        "config_fingerprint": config_fingerprint,
        "time_mapping_version": time_mapping_version,
        "created_at": _instant(now),
        "payload_json": raw,
        "payload_hash": _hash(raw),
    }
    _decode_input(row)
    with _write(data_dir, len(raw.encode())) as c:
        old = _row(
            c.execute(
                "SELECT * FROM analysis_inputs WHERE night_date=? AND input_hash=? "
                "AND source_fingerprint=? "
                "AND prompt_hash=? AND schema_hash=? AND config_fingerprint=?",
                tuple(
                    row[k]
                    for k in (
                        "night_date",
                        "input_hash",
                        "source_fingerprint",
                        "prompt_hash",
                        "schema_hash",
                        "config_fingerprint",
                    )
                ),
            )
        )
        if old:
            decoded = _decode_input(old)
            if any(
                row[k] != old[k] for k in ("prompt_version", "model_ref", "time_mapping_version")
            ):
                _fail("sleep_analysis_input_conflict")
            return decoded
        c.execute(
            "INSERT INTO analysis_inputs VALUES (" + ",".join("?" for _ in row) + ")",
            tuple(row.values()),
        )
    return _decode_input(row)


def begin_attempt(data_dir, input_id, *, request_id, now=None, revise_attempt=None):
    request_id = _check(request_id, _ID)
    with _write(data_dir) as c:
        source = _input(c, input_id)
        if source is None:
            _fail("sleep_analysis_input_missing")
        old = _row(c.execute("SELECT * FROM analysis_attempts WHERE request_id=?", (request_id,)))
        if old:
            result = _decode_attempt(old, _input(c, old["input_id"]))
            if result["input_id"] != input_id:
                _fail("sleep_analysis_request_conflict")
            return result
        published = _report(c, source["night_date"])
        if (published["attempt_id"] if published else None) != revise_attempt:
            _fail("sleep_analysis_already_published")
        row = {
            "attempt_id": uuid4().hex,
            "input_id": input_id,
            "night_date": source["night_date"],
            "request_id": request_id,
            "started_at": _instant(now),
            "finished_at": None,
            "state": "running",
            "error_code": None,
            "retry_after": None,
        }
        _decode_attempt(row, source)
        c.execute(
            "INSERT INTO analysis_attempts VALUES (" + ",".join("?" for _ in row) + ")",
            tuple(row.values()),
        )
    return row | {"input_hash": source["input_hash"]}


def finish_attempt(data_dir, attempt_id, *, state, error_code=None, retry_after=None, now=None):
    if state not in STATES - {"running", "complete"} or error_code not in ERROR_CODES:
        _fail("sleep_analysis_attempt_invalid")
    with _write(data_dir) as c:
        old = _attempt(c, attempt_id)
        if old is None:
            _fail("sleep_analysis_attempt_missing")
        if old["state"] != "running":
            if (old["state"], old["error_code"], old["retry_after"]) != (
                state,
                error_code,
                retry_after,
            ):
                _fail("sleep_analysis_attempt_conflict")
            return old
        new = old | {
            "state": state,
            "error_code": error_code,
            "retry_after": retry_after,
            "finished_at": _instant(now),
        }
        _decode_attempt(new, _input(c, old["input_id"]))
        c.execute(
            "UPDATE analysis_attempts SET state=?,error_code=?,retry_after=?,finished_at=? "
            "WHERE attempt_id=? AND state='running'",
            (state, error_code, retry_after, new["finished_at"], attempt_id),
        )
        return new


def publish_report(
    data_dir, attempt_id, output, *, model_metadata=None, guard=None, now=None, revise_attempt=None
):
    """Finish the attempt and publish exactly one report per night atomically."""
    model_raw = _model_metadata({} if model_metadata is None else model_metadata)
    with _write(data_dir, MAX_OUTPUT_BYTES) as c:
        attempt = _attempt(c, attempt_id)
        if attempt is None:
            _fail("sleep_analysis_attempt_missing")
        source = _input(c, attempt["input_id"])
        raw = _output_payload(output, source["payload"])
        old = _report(c, attempt["night_date"])
        if revise_attempt is not None and (not old or old["attempt_id"] != revise_attempt):
            _fail("sleep_analysis_source_changed")
        if old and revise_attempt is None:
            if (
                old["attempt_id"] == attempt_id
                and old["payload_hash"] == _hash(raw)
                and (model_metadata is None or old["model_hash"] == _hash(model_raw))
            ):
                return old
            _fail("sleep_analysis_already_published")
        if attempt["state"] != "running":
            _fail("sleep_analysis_attempt_conflict")
        # The caller reads/rechecks source facts through this exact connection.
        # BEGIN IMMEDIATE prevents another writer changing facts between this
        # predicate and publication; external model calls have already finished.
        if guard is not None and guard(c) is not True:
            _fail("sleep_analysis_source_changed")
        finished = _instant(now)
        _decode_attempt(attempt | {"state": "complete", "finished_at": finished}, source)
        c.execute(
            "UPDATE analysis_attempts SET state='complete',finished_at=? "
            "WHERE attempt_id=? AND state='running'",
            (finished, attempt_id),
        )
        table = "sleep_analysis_reports"
        if revise_attempt is not None:
            if not revisions_available(c):
                for sql in _REVISION_DDL.values():
                    c.execute(sql)
            table = REVISION_TABLE
        c.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
            (
                attempt["night_date"],
                attempt_id,
                finished,
                raw,
                _hash(raw),
                model_raw,
                _hash(model_raw),
            ),
        )
        return _report(c, attempt["night_date"])


def validate_reports(connection):
    """Backup validation covers private snapshots, hashes and publication references."""
    if not report_available(connection):
        return
    for (input_id,) in connection.execute("SELECT input_id FROM analysis_inputs"):
        _input(connection, input_id)
    for (attempt_id,) in connection.execute("SELECT attempt_id FROM analysis_attempts"):
        _attempt(connection, attempt_id)
    tables = ["sleep_analysis_reports"]
    if revisions_available(connection):
        tables.append(REVISION_TABLE)
    for table in tables:
        for row in connection.execute(f"SELECT * FROM {table}"):
            entry = dict(
                zip(
                    (
                        "night_date",
                        "attempt_id",
                        "generated_at",
                        "payload_json",
                        "payload_hash",
                        "model_json",
                        "model_hash",
                    ),
                    row,
                    strict=True,
                )
            )
            _decode_report(connection, entry, entry["night_date"])
