"""Explicit schema, immutable review evidence and durable attempt history."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from sleepscape.domain.sleep_features import canonical_json
from sleepscape.models.audit import safe_usage
from sleepscape.models.base import ERROR_CODES
from sleepscape.resources.weekly_review import (
    MAX_EVIDENCE_BYTES,
    validate_evidence,
    validate_output,
)
from sleepscape.storage.usage import require_capacity

_STATES = (
    "incomplete_data",
    "readiness_unavailable",
    "blocked",
    "running",
    "failed",
    "interrupted",
    "source_changed",
    "complete",
)
STORAGE_CODES = ERROR_CODES | {
    "review_input_too_large",
    "review_deadline",
    "review_readiness_unavailable",
    "review_source_changed",
    "review_interrupted",
}
_HEX = re.compile("[0-9a-f]{64}")
_REQUEST = re.compile("[0-9a-f]{32}")
_FIXED = re.compile("[a-z][a-z_0-9.]{0,100}")
_DDL = {
    "sleep_review_schema": """CREATE TABLE sleep_review_schema (
      version INTEGER PRIMARY KEY CHECK(version=1))""",
    "sleep_review_inputs": """CREATE TABLE sleep_review_inputs (
      input_id TEXT PRIMARY KEY, end_date TEXT NOT NULL, source_hash TEXT NOT NULL,
      config_hash TEXT NOT NULL, contract_hash TEXT NOT NULL, payload TEXT NOT NULL,
      UNIQUE(input_id,end_date))""",
    "sleep_review_runs": f"""CREATE TABLE sleep_review_runs (
      end_date TEXT PRIMARY KEY, start_date TEXT NOT NULL, checked_at TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ({",".join(repr(s) for s in _STATES)})),
      incomplete TEXT NOT NULL, error_code TEXT, input_id TEXT,
      request_id TEXT UNIQUE, UNIQUE(end_date,request_id,input_id),
      FOREIGN KEY(input_id,end_date) REFERENCES sleep_review_inputs(input_id,end_date))""",
    "sleep_review_reports": """CREATE TABLE sleep_review_reports (
      end_date TEXT PRIMARY KEY, input_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
      generated_at TEXT NOT NULL, content TEXT NOT NULL, metadata TEXT NOT NULL,
      report_hash TEXT NOT NULL,
      FOREIGN KEY(end_date,request_id,input_id)
        REFERENCES sleep_review_runs(end_date,request_id,input_id))""",
}
TABLES = tuple(_DDL)
for table in ("sleep_review_inputs", "sleep_review_reports", "sleep_review_runs"):
    operations = ("DELETE",) if table == "sleep_review_runs" else ("UPDATE", "DELETE")
    for operation in operations:
        name = f"{table}_no_{operation.lower()}"
        _DDL[name] = (
            f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'sleep_review_immutable'); END"
        )
_DDL["sleep_review_run_terminal"] = """CREATE TRIGGER sleep_review_run_terminal
  BEFORE UPDATE ON sleep_review_runs WHEN OLD.state IN
  ('failed','interrupted','source_changed','complete')
  OR (OLD.request_id IS NOT NULL AND
      (NEW.request_id IS NOT OLD.request_id OR NEW.input_id IS NOT OLD.input_id
       OR NEW.start_date IS NOT OLD.start_date OR NEW.end_date IS NOT OLD.end_date
       OR NEW.checked_at IS NOT OLD.checked_at OR NEW.state='blocked'))
  BEGIN SELECT RAISE(ABORT,'sleep_review_immutable'); END"""
_HISTORY_TABLE = "sleep_review_failed_attempts"
_RETRY_DDL = {
    _HISTORY_TABLE: """CREATE TABLE sleep_review_failed_attempts (
      request_id TEXT PRIMARY KEY NOT NULL, end_date TEXT NOT NULL,
      input_id TEXT NOT NULL, checked_at TEXT NOT NULL, error_code TEXT NOT NULL,
      FOREIGN KEY(input_id,end_date) REFERENCES sleep_review_inputs(input_id,end_date))""",
    "sleep_review_run_terminal": """CREATE TRIGGER sleep_review_run_terminal
  BEFORE UPDATE ON sleep_review_runs WHEN NOT (
       OLD.state='failed' AND NEW.state='running' AND NEW.request_id IS NULL
       AND NEW.end_date=OLD.end_date AND EXISTS (
         SELECT 1 FROM sleep_review_failed_attempts h WHERE h.request_id=OLD.request_id
           AND h.end_date=OLD.end_date AND h.input_id=OLD.input_id
           AND h.checked_at=OLD.checked_at AND h.error_code=OLD.error_code)) AND (
      OLD.state IN ('failed','interrupted','source_changed','complete')
      OR (OLD.request_id IS NOT NULL AND
          (NEW.request_id IS NOT OLD.request_id OR NEW.input_id IS NOT OLD.input_id
           OR NEW.start_date IS NOT OLD.start_date OR NEW.end_date IS NOT OLD.end_date
           OR NEW.checked_at IS NOT OLD.checked_at OR NEW.state='blocked')))
  BEGIN SELECT RAISE(ABORT,'sleep_review_immutable'); END""",
}
for _operation in ("UPDATE", "DELETE"):
    _name = f"{_HISTORY_TABLE}_no_{_operation.lower()}"
    _RETRY_DDL[_name] = (
        f"CREATE TRIGGER {_name} BEFORE {_operation} ON {_HISTORY_TABLE} "
        "BEGIN SELECT RAISE(ABORT,'sleep_review_immutable'); END"
    )


class ReviewStorageError(ValueError):
    def __init__(self, code="sleep_review_state_unavailable"):
        super().__init__(code)
        self.__suppress_context__ = True


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def schema_version(connection):
    if connection is None:
        return 0
    updated = _DDL | _RETRY_DDL
    placeholders = ",".join("?" for _ in updated)
    found = dict(
        connection.execute(
            f"SELECT name,sql FROM sqlite_master WHERE name IN ({placeholders})", tuple(updated)
        )
    )
    if not found:
        return 0
    if found not in (_DDL, updated) or [
        r[0] for r in connection.execute("SELECT version FROM sleep_review_schema")
    ] != [1]:
        raise ReviewStorageError("sleep_review_schema_invalid")
    return 2 if found == updated else 1


def available(connection):
    return schema_version(connection) != 0


def table_names(connection):
    version = schema_version(connection)
    if version == 2:
        return TABLES + (_HISTORY_TABLE,)
    return TABLES if version else ()


@contextmanager
def connection(data_dir):
    try:
        path = Path(data_dir) / "sleepscape.sqlite3"
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
            raise ReviewStorageError("sleep_review_not_initialized")
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db
    except (sqlite3.Error, OSError):
        raise ReviewStorageError() from None


def migrate(data_dir):
    require_capacity(data_dir, "write")
    with connection(data_dir) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = schema_version(db)
        if version == 0:
            for statement in _DDL.values():
                db.execute(statement)
            db.execute("INSERT INTO sleep_review_schema VALUES(1)")
        if version < 2:
            db.execute("DROP TRIGGER sleep_review_run_terminal")
            for statement in _RETRY_DDL.values():
                db.execute(statement)


def load_run(db, end):
    row = db.execute("SELECT * FROM sleep_review_runs WHERE end_date=?", (end,)).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["incomplete"] = _missing(value)
    _stamp(value["checked_at"])
    return value


def _result(db, end, start, checked_at, state, incomplete, code, input_id):
    changed = db.execute(
        """INSERT INTO sleep_review_runs
        (end_date,start_date,checked_at,state,incomplete,error_code,input_id)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(end_date) DO UPDATE SET
        start_date=excluded.start_date,checked_at=excluded.checked_at,state=excluded.state,
        incomplete=excluded.incomplete,error_code=excluded.error_code,input_id=excluded.input_id
        WHERE sleep_review_runs.state IN ('incomplete_data','readiness_unavailable','blocked')
        AND sleep_review_runs.request_id IS NULL""",
        (end, start, checked_at, state, canonical_json(incomplete), code, input_id),
    ).rowcount
    return changed == 1


def record_check(directory, days, checked_at, state, *, incomplete=None, code=None):
    require_capacity(directory, "write")
    with connection(directory) as db, db:
        db.execute("BEGIN IMMEDIATE")
        return _result(db, days[-1], days[0], checked_at, state, incomplete or [], code, None)


def claim(directory, evidence, source_hash, config_hash, contract_hash, checked_at):
    require_capacity(directory, "model")
    validate_evidence(evidence)
    start, end = (evidence["window"][key] for key in ("start_date", "end_date"))
    values = [end, source_hash, config_hash, contract_hash, evidence]
    input_id = digest(values)
    with connection(directory) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO sleep_review_inputs VALUES(?,?,?,?,?,?)",
            (input_id, *values[:-1], canonical_json(evidence)),
        )
        previous = load_run(db, end)
        if previous and previous["state"] == "failed" and previous["request_id"] is not None:
            db.execute(
                "INSERT INTO sleep_review_failed_attempts "
                "SELECT request_id,end_date,input_id,checked_at,error_code "
                "FROM sleep_review_runs WHERE end_date=?",
                (end,),
            )
            db.execute(
                "UPDATE sleep_review_runs SET start_date=?,checked_at=?,state='running',"
                "incomplete='[]',error_code=NULL,input_id=?,request_id=NULL WHERE end_date=?",
                (start, checked_at, input_id, end),
            )
        elif not _result(db, end, start, checked_at, "running", [], None, input_id):
            db.rollback()
            return None
        return input_id


def attach_request(directory, end, request_id):
    with connection(directory) as db, db:
        if (
            db.execute(
                "UPDATE sleep_review_runs SET request_id=? WHERE end_date=? "
                "AND state='running' AND request_id IS NULL",
                (request_id, end),
            ).rowcount
            != 1
        ):
            raise ReviewStorageError()


def finish(directory, end, state, code):
    with connection(directory) as db, db:
        db.execute(
            "UPDATE sleep_review_runs SET state=?,error_code=? "
            "WHERE end_date=? AND state='running'",
            (state, code if code in STORAGE_CODES else "model_operation_failed", end),
        )


def _version(db):
    return db.execute("PRAGMA data_version").fetchone()[0]


def publish(directory, end, input_id, request_id, generated_at, content, metadata, guard):
    """Compute the guard without a write lock, then compare the same connection's version."""
    require_capacity(directory, "write")
    with connection(directory) as db:
        for _ in range(2):
            version = _version(db)
            db.execute("BEGIN")
            matched = guard(db)
            db.commit()
            if _version(db) != version:
                continue
            if not matched:
                return False
            with db:
                db.execute("BEGIN IMMEDIATE")
                if _version(db) != version:
                    continue
                if (
                    db.execute(
                        "UPDATE sleep_review_runs SET state='complete' "
                        "WHERE end_date=? AND state='running' "
                        "AND request_id=? AND input_id=?",
                        (end, request_id, input_id),
                    ).rowcount
                    != 1
                ):
                    raise ReviewStorageError()
                values = [end, input_id, request_id, generated_at, content, metadata]
                db.execute(
                    "INSERT INTO sleep_review_reports VALUES(?,?,?,?,?,?,?)",
                    (
                        *values[:4],
                        canonical_json(content),
                        canonical_json(metadata),
                        digest(values),
                    ),
                )
                return True
    return False


def _decode(raw, limit):
    if not isinstance(raw, str) or len(raw.encode()) > limit:
        raise ReviewStorageError()
    value = json.loads(raw)
    if canonical_json(value) != raw:
        raise ReviewStorageError()
    return value


def _missing(row):
    failures = _decode(row["incomplete"], 96 * 1024)
    start, end = (date.fromisoformat(row[key]) for key in ("start_date", "end_date"))
    if not isinstance(failures, list) or len(failures) > 7 or not 0 <= (end - start).days < 7:
        raise ReviewStorageError()
    dates = [item["date"] for item in failures]
    if (
        dates != sorted(set(dates))
        or any(not start <= date.fromisoformat(day) <= end for day in dates)
        or (row["state"] == "incomplete_data") != bool(dates)
    ):
        raise ReviewStorageError()
    for entry in failures:
        if (
            set(entry) != {"date", "missing"}
            or not isinstance(entry["missing"], list)
            or not 1 <= len(entry["missing"]) <= 128
        ):
            raise ReviewStorageError()
        for item in entry["missing"]:
            if set(item) != {"source", "metric", "code"} or any(
                not _FIXED.fullmatch(v) for v in item.values()
            ):
                raise ReviewStorageError()
    return failures


def _input(row):
    values = [row[k] for k in ("end_date", "source_hash", "config_hash", "contract_hash")]
    if any(not _HEX.fullmatch(v) for v in [*values[1:], row["input_id"]]):
        raise ReviewStorageError()
    evidence = validate_evidence(_decode(row["payload"], MAX_EVIDENCE_BYTES))
    if (
        evidence["window"]["end_date"] != values[0]
        or digest([*values, evidence]) != row["input_id"]
    ):
        raise ReviewStorageError()
    return evidence


def load_report(db, end):
    row = db.execute(
        "SELECT r.*, i.payload, i.source_hash, i.config_hash, i.contract_hash "
        "FROM sleep_review_reports r JOIN sleep_review_inputs i USING(input_id,end_date) "
        "WHERE r.end_date=?",
        (end,),
    ).fetchone()
    if not row:
        return None
    evidence = _input(row)
    content = _decode(row["content"], MAX_EVIDENCE_BYTES)
    metadata = _decode(row["metadata"], 4096)
    if (
        digest([end, row["input_id"], row["request_id"], row["generated_at"], content, metadata])
        != row["report_hash"]
    ):
        raise ReviewStorageError()
    _stamp(row["generated_at"])
    if (
        set(metadata) != {"model", "usage", "duration_ms"}
        or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,128}", metadata["model"])
        or type(metadata["duration_ms"]) is not int
        or not 0 <= metadata["duration_ms"] <= 300_000
        or not isinstance(metadata["usage"], dict)
        or metadata["usage"] != safe_usage(metadata["usage"])
    ):
        raise ReviewStorageError()
    validate_output(content, evidence)
    return {
        "generated_at": row["generated_at"],
        "evidence": evidence,
        "content": content,
        "source_hash": row["source_hash"],
    }


def _stamp(value):
    if datetime.fromisoformat(value).utcoffset() != timedelta(0):
        raise ReviewStorageError()


def validate(db):
    """Validate the optional tables, including historical unreferenced valid inputs."""
    previous_factory = db.row_factory
    db.row_factory = sqlite3.Row
    try:
        version = schema_version(db)
        if not version:
            raise ReviewStorageError()
        for row in db.execute("SELECT * FROM sleep_review_inputs"):
            _input(row)
        for row in db.execute("SELECT * FROM sleep_review_runs"):
            _stamp(row["checked_at"])
            _missing(row)
            request = row["request_id"]
            input_id = row["input_id"]
            state = row["state"]
            if (
                (row["error_code"] is not None and row["error_code"] not in STORAGE_CODES)
                or (request is not None and (not _REQUEST.fullmatch(request) or not input_id))
                or (state in {"running", "complete", "source_changed"} and not input_id)
                or (state in {"complete", "source_changed"} and not request)
            ):
                raise ReviewStorageError()
            if (load_report(db, row["end_date"]) is not None) != (state == "complete"):
                raise ReviewStorageError()
        if version == 2:
            history = db.execute(
                "SELECT h.*, r.checked_at AS current_at, r.request_id AS current_request "
                "FROM sleep_review_failed_attempts h LEFT JOIN sleep_review_runs r USING(end_date)"
            )
            for row in history:
                _stamp(row["checked_at"])
                if (
                    row["current_at"] is None
                    or datetime.fromisoformat(row["checked_at"])
                    > datetime.fromisoformat(row["current_at"])
                    or not _REQUEST.fullmatch(row["request_id"])
                    or row["request_id"] == row["current_request"]
                    or row["error_code"] not in STORAGE_CODES
                ):
                    raise ReviewStorageError()
    finally:
        db.row_factory = previous_factory
