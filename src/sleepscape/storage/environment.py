"""Explicit environment migration and transactional append-only device observations."""

import hashlib
import hmac
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sleepscape.domain.environment import (
    EnvironmentBatch,
    MeasurementProfile,
    canonical_json,
    content_hash,
    validate_light_profile,
)

ENVIRONMENT_TABLES = (
    "environment_schema",
    "environment_devices",
    "environment_profiles",
    "environment_records",
    "environment_time_anchors",
    "environment_device_status",
    "environment_ingest_batches",
)
PAYLOAD_TABLES = (
    "environment_profiles",
    "environment_records",
    "environment_time_anchors",
    "environment_device_status",
)
_SCHEMA = """
CREATE TABLE environment_schema (version INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE environment_devices (
 device_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE environment_profiles (
 device_id TEXT NOT NULL REFERENCES environment_devices(device_id), profile_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(device_id, profile_id)
);
CREATE TABLE environment_records (
 device_id TEXT NOT NULL, boot_id TEXT NOT NULL, record_type TEXT NOT NULL,
 seq INTEGER NOT NULL, profile_id TEXT NOT NULL, mono_start_ms INTEGER NOT NULL,
 mono_end_ms INTEGER NOT NULL, payload_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 received_at TEXT NOT NULL, PRIMARY KEY(device_id, boot_id, record_type, seq),
 FOREIGN KEY(device_id, profile_id) REFERENCES environment_profiles(device_id, profile_id)
);
CREATE INDEX environment_records_clock ON environment_records(device_id,boot_id,mono_start_ms);
CREATE TABLE environment_time_anchors (
 device_id TEXT NOT NULL REFERENCES environment_devices(device_id), boot_id TEXT NOT NULL,
 anchor_id TEXT NOT NULL, monotonic_ms INTEGER NOT NULL, utc_at TEXT NOT NULL,
 payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, received_at TEXT NOT NULL,
 PRIMARY KEY(device_id,boot_id,anchor_id)
);
CREATE INDEX environment_anchors_clock ON environment_time_anchors(device_id,boot_id,monotonic_ms);
CREATE TABLE environment_device_status (
 device_id TEXT NOT NULL REFERENCES environment_devices(device_id), boot_id TEXT NOT NULL,
 status_seq INTEGER NOT NULL, monotonic_ms INTEGER NOT NULL, payload_json TEXT NOT NULL,
 content_hash TEXT NOT NULL, received_at TEXT NOT NULL,
 PRIMARY KEY(device_id,boot_id,status_seq)
);
CREATE TABLE environment_ingest_batches (
 id INTEGER PRIMARY KEY, device_id TEXT NOT NULL REFERENCES environment_devices(device_id),
 boot_id TEXT NOT NULL, received_at TEXT NOT NULL, content_hash TEXT NOT NULL,
 accepted_count INTEGER NOT NULL, duplicate_count INTEGER NOT NULL
);
"""


class EnvironmentError(Exception):
    def __init__(self, code: str, status: int = 503):
        super().__init__(code)
        self.status = status
        self.__suppress_context__ = True


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _path(data_dir: Path) -> Path:
    path = Path(data_dir) / "sleepscape.sqlite3"
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise EnvironmentError("environment_not_initialized")
    return path


def _connect(data_dir: Path, *, writable: bool = False) -> sqlite3.Connection:
    path = _path(data_dir)
    connection = sqlite3.connect(
        path.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=3
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if not writable:
        connection.execute("PRAGMA query_only=ON")
    try:
        rows = connection.execute("SELECT version FROM environment_schema").fetchall()
        if [row[0] for row in rows] != [1]:
            raise EnvironmentError("environment_schema_unsupported")
    except Exception:
        connection.close()
        raise EnvironmentError("environment_not_initialized") from None
    return connection


def _secure(data_dir: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(data_dir) / ("sleepscape.sqlite3" + suffix)
        if path.exists():
            path.chmod(0o600)


def initialize(data_dir: Path) -> None:
    """Called only by explicit registration, never by startup, GET, or unauthenticated POST."""
    from sleepscape.storage.repository import Repository

    data_dir = Path(data_dir)
    if data_dir.is_symlink():
        raise EnvironmentError("environment_directory_symlink_not_allowed")
    if not (data_dir / "sleepscape.sqlite3").exists():
        with Repository(data_dir):
            pass
    path = _path(data_dir)
    with closing(sqlite3.connect(path, timeout=3)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"runs", "raw_snapshots", "requests", "normalized_records"} <= tables:
            raise EnvironmentError("environment_base_schema_invalid")
        present = set(ENVIRONMENT_TABLES) & tables
        if present:
            if present != set(ENVIRONMENT_TABLES):
                raise EnvironmentError("environment_schema_incomplete")
            if connection.execute("SELECT version FROM environment_schema").fetchall() != [(1,)]:
                raise EnvironmentError("environment_schema_unsupported")
            return
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute("INSERT INTO environment_schema VALUES (1, ?)", (_now(),))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    _secure(data_dir)


def register(
    data_dir: Path, device_id: str, profile_id: str, profile: MeasurementProfile, token: str
) -> None:
    initialize(data_dir)
    with closing(_connect(data_dir, writable=True)) as connection, connection:
        if connection.execute(
            "SELECT 1 FROM environment_devices WHERE device_id=?", (device_id,)
        ).fetchone():
            raise EnvironmentError("device_already_registered", 409)
        now = _now()
        connection.execute(
            "INSERT INTO environment_devices VALUES (?, ?, 1, ?, ?)",
            (device_id, hashlib.sha256(token.encode()).hexdigest(), now, now),
        )
        payload = profile.model_dump(mode="json", exclude_none=True)
        connection.execute(
            "INSERT INTO environment_profiles VALUES (?, ?, ?, ?, ?)",
            (device_id, profile_id, canonical_json(payload), content_hash(payload), now),
        )
    _secure(data_dir)


def add_profile(
    data_dir: Path, device_id: str, profile_id: str, profile: MeasurementProfile
) -> None:
    with closing(_connect(data_dir, writable=True)) as connection, connection:
        if not connection.execute(
            "SELECT 1 FROM environment_devices WHERE device_id=?", (device_id,)
        ).fetchone():
            raise EnvironmentError("device_not_registered", 404)
        payload = profile.model_dump(mode="json", exclude_none=True)
        existing = connection.execute(
            "SELECT content_hash FROM environment_profiles WHERE device_id=? AND profile_id=?",
            (device_id, profile_id),
        ).fetchone()
        if existing:
            if existing[0] != content_hash(payload):
                raise EnvironmentError("profile_conflict", 409)
            return
        connection.execute(
            "INSERT INTO environment_profiles VALUES (?, ?, ?, ?, ?)",
            (device_id, profile_id, canonical_json(payload), content_hash(payload), _now()),
        )
    _secure(data_dir)


def revoke(data_dir: Path, device_id: str) -> None:
    with closing(_connect(data_dir, writable=True)) as connection, connection:
        result = connection.execute(
            "UPDATE environment_devices SET enabled=0, updated_at=? WHERE device_id=?",
            (_now(), device_id),
        )
        if result.rowcount != 1:
            raise EnvironmentError("device_not_registered", 404)
    _secure(data_dir)


def authenticate(data_dir: Path, token: str) -> str | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    with closing(_connect(data_dir)) as connection:
        row = connection.execute(
            "SELECT device_id, token_hash FROM environment_devices "
            "WHERE token_hash=? AND enabled=1",
            (digest,),
        ).fetchone()
        if row and hmac.compare_digest(row["token_hash"], digest):
            return row["device_id"]
    return None


def _append(connection, table: str, identity: dict, fields: dict, payload: dict, code: str) -> str:
    where = " AND ".join(f"{name}=?" for name in identity)
    row = connection.execute(
        f"SELECT content_hash FROM {table} WHERE {where}", tuple(identity.values())
    ).fetchone()
    digest = content_hash(payload)
    if row:
        if row[0] != digest:
            raise EnvironmentError(code, 409)
        return "duplicate"
    values = {**identity, **fields, "payload_json": canonical_json(payload), "content_hash": digest}
    names = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(values.values())
    )
    return "accepted"


def ingest(data_dir: Path, device_id: str, batch: EnvironmentBatch) -> dict:
    if batch.device_id != device_id:
        raise EnvironmentError("device_binding_mismatch", 403)
    received = _now()
    ack = {
        "schema_version": 1,
        "device_id": device_id,
        "boot_id": batch.boot_id,
        "received_at": received,
        "records": [],
        "time_anchors": [],
        "device_status": None,
    }
    statuses = []
    with closing(_connect(data_dir, writable=True)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            enabled = connection.execute(
                "SELECT enabled FROM environment_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if not enabled or not enabled[0]:
                raise EnvironmentError("device_authentication_required", 401)
            profiles = {}
            for record in batch.records:
                if record.measurement_profile_id not in profiles:
                    row = connection.execute(
                        "SELECT payload_json FROM environment_profiles "
                        "WHERE device_id=? AND profile_id=?",
                        (device_id, record.measurement_profile_id),
                    ).fetchone()
                    if row is None:
                        raise EnvironmentError("measurement_profile_not_registered", 422)
                    profiles[record.measurement_profile_id] = (
                        MeasurementProfile.model_validate_json(row[0])
                    )
                try:
                    validate_light_profile(record, profiles[record.measurement_profile_id])
                except ValueError as exc:
                    raise EnvironmentError(str(exc), 422) from None
                if (
                    record.record_type == "event"
                    and record.threshold_profile_id
                    != profiles[record.measurement_profile_id].threshold_profile_id
                ):
                    raise EnvironmentError("threshold_profile_mismatch", 422)
                state = _append(
                    connection,
                    "environment_records",
                    {
                        "device_id": device_id,
                        "boot_id": batch.boot_id,
                        "record_type": record.record_type,
                        "seq": record.seq,
                    },
                    {
                        "profile_id": record.measurement_profile_id,
                        "mono_start_ms": record.mono_start_ms,
                        "mono_end_ms": record.mono_end_ms,
                        "received_at": received,
                    },
                    record.model_dump(mode="json"),
                    "record_conflict",
                )
                statuses.append(state)
                ack["records"].append(
                    {"record_type": record.record_type, "seq": record.seq, "status": state}
                )
            for anchor in batch.time_anchors:
                state = _append(
                    connection,
                    "environment_time_anchors",
                    {
                        "device_id": device_id,
                        "boot_id": batch.boot_id,
                        "anchor_id": anchor.anchor_id,
                    },
                    {
                        "monotonic_ms": anchor.monotonic_ms,
                        "utc_at": anchor.utc_at,
                        "received_at": received,
                    },
                    anchor.model_dump(mode="json"),
                    "anchor_conflict",
                )
                statuses.append(state)
                ack["time_anchors"].append({"anchor_id": anchor.anchor_id, "status": state})
            if batch.device_status:
                item = batch.device_status
                state = _append(
                    connection,
                    "environment_device_status",
                    {
                        "device_id": device_id,
                        "boot_id": batch.boot_id,
                        "status_seq": item.status_seq,
                    },
                    {"monotonic_ms": item.monotonic_ms, "received_at": received},
                    item.model_dump(mode="json"),
                    "status_conflict",
                )
                statuses.append(state)
                ack["device_status"] = {"status_seq": item.status_seq, "status": state}
            connection.execute(
                "INSERT INTO environment_ingest_batches "
                "(device_id,boot_id,received_at,content_hash,"
                "accepted_count,duplicate_count) VALUES (?,?,?,?,?,?)",
                (
                    device_id,
                    batch.boot_id,
                    received,
                    content_hash(batch.model_dump(mode="json")),
                    statuses.count("accepted"),
                    statuses.count("duplicate"),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    _secure(data_dir)
    return ack


def status(data_dir: Path) -> dict:
    try:
        with closing(_connect(data_dir)) as connection:
            counts = {
                name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ENVIRONMENT_TABLES
            }
            latest = connection.execute(
                "SELECT MAX(received_at) FROM environment_ingest_batches"
            ).fetchone()[0]
            return {"state": "initialized", "counts": counts, "last_received_at": latest}
    except EnvironmentError:
        return {"state": "not_initialized", "counts": {}, "last_received_at": None}


def rotate_token(data_dir: Path, device_id: str, token: str) -> None:
    with closing(_connect(data_dir, writable=True)) as connection, connection:
        result = connection.execute(
            "UPDATE environment_devices SET token_hash=?,enabled=1,updated_at=? WHERE device_id=?",
            (hashlib.sha256(token.encode()).hexdigest(), _now(), device_id),
        )
        if result.rowcount != 1:
            raise EnvironmentError("device_not_registered", 404)
    _secure(data_dir)
