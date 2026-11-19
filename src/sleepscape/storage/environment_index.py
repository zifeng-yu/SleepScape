"""Explicit, rebuildable UTC projections of immutable environment observations.

Ingestion updates a boot in its existing transaction only after this schema has
been explicitly installed. Readers never migrate or repair it. Revision triggers
also make interrupted backfills and out-of-band inserts visible as stale instead
of silently serving an old mapping. Compatible anchors do not rewrite history.
"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sleepscape.domain.environment import TimeAnchor, canonical_json
from sleepscape.domain.environment_integrity import validate_environment_row
from sleepscape.domain.environment_time import (
    MAX_UTC_US,
    MIN_UTC_US,
    TIME_MAPPING_ALGORITHM,
    BootTimeMapper,
    build_time_mapper,
    utc_to_us,
)
from sleepscape.storage.environment import EnvironmentError, _connect, _secure

INDEX_TABLES = (
    "environment_index_schema",
    "environment_index_boots",
    "environment_time_index",
)
INDEX_VERSION = 1
_SCHEMA = """
CREATE TABLE environment_index_schema (
 version INTEGER PRIMARY KEY, algorithm TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE environment_index_boots (
 device_id TEXT NOT NULL REFERENCES environment_devices(device_id), boot_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL DEFAULT 0,
 indexed_revision INTEGER NOT NULL DEFAULT 0,
 mapping_version INTEGER NOT NULL DEFAULT 0,
 anchor_revision INTEGER NOT NULL DEFAULT 0,
 indexed_anchor_revision INTEGER NOT NULL DEFAULT 0,
 mapping_json TEXT, indexed_at TEXT,
 record_count INTEGER NOT NULL DEFAULT 0,
 unknown_count INTEGER NOT NULL DEFAULT 0,
 conflict_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(device_id,boot_id),
 CHECK(0 <= indexed_revision AND indexed_revision <= source_revision),
 CHECK(0 <= indexed_anchor_revision AND indexed_anchor_revision <= anchor_revision),
 CHECK(record_count >= 0 AND unknown_count >= 0 AND conflict_count >= 0)
);
CREATE TABLE environment_time_index (
 device_id TEXT NOT NULL, boot_id TEXT NOT NULL, record_type TEXT NOT NULL, seq INTEGER NOT NULL,
 profile_id TEXT NOT NULL, utc_start_us INTEGER, utc_end_us INTEGER,
 time_quality TEXT NOT NULL CHECK(time_quality IN ('synced','estimated','unknown','conflict')),
 time_uncertainty_ms REAL, anchor_ids_json TEXT NOT NULL, quality_json TEXT NOT NULL,
 mapping_version INTEGER NOT NULL,
 PRIMARY KEY(device_id,boot_id,record_type,seq),
 FOREIGN KEY(device_id,boot_id,record_type,seq)
 REFERENCES environment_records(device_id,boot_id,record_type,seq),
 FOREIGN KEY(device_id,boot_id) REFERENCES environment_index_boots(device_id,boot_id),
 FOREIGN KEY(device_id,profile_id) REFERENCES environment_profiles(device_id,profile_id),
 CHECK((utc_start_us IS NULL AND utc_end_us IS NULL AND time_quality IN ('unknown','conflict'))
 OR (utc_start_us IS NOT NULL AND utc_end_us IS NOT NULL AND utc_start_us < utc_end_us
 AND time_quality IN ('synced','estimated')))
);
CREATE INDEX environment_time_index_utc ON environment_time_index(utc_start_us,utc_end_us);
CREATE INDEX environment_time_index_device_utc
 ON environment_time_index(device_id,utc_start_us,utc_end_us);
CREATE INDEX environment_time_index_quality
 ON environment_time_index(device_id,boot_id,time_quality);
CREATE INDEX environment_time_index_boot_utc
 ON environment_time_index(device_id,boot_id,record_type,utc_start_us DESC,utc_end_us);
CREATE INDEX environment_ingest_device_received
 ON environment_ingest_batches(device_id,received_at DESC,id DESC);
CREATE INDEX environment_status_device_received
 ON environment_device_status(device_id,received_at DESC,status_seq DESC);
"""
_TRACKED = (
    ("environment_records", "records", "record_count=record_count+1"),
    ("environment_time_anchors", "anchors", "anchor_revision=anchor_revision+1"),
    ("environment_device_status", "status", None),
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _rows(cursor):
    columns = [column[0] for column in cursor.description]
    for row in cursor:
        yield dict(zip(columns, row, strict=True))


def index_available(connection: sqlite3.Connection) -> bool:
    """Validate the optional schema without creating anything."""
    present = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
            INDEX_TABLES,
        )
    }
    if not present:
        return False
    if present != set(INDEX_TABLES):
        raise EnvironmentError("environment_index_schema_incomplete")
    versions = connection.execute(
        "SELECT version,algorithm FROM environment_index_schema"
    ).fetchall()
    if [tuple(row) for row in versions] != [(INDEX_VERSION, TIME_MAPPING_ALGORITHM)]:
        raise EnvironmentError("environment_index_schema_unsupported")
    triggers = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'environment_index_%'"
        )
    }
    if not {f"environment_index_{name}_insert" for _, name, _ in _TRACKED} <= triggers:
        raise EnvironmentError("environment_index_triggers_missing")
    return True


def migrate_index(data_dir: Path) -> dict:
    """Explicit schema migration; preserves all raw tables and never creates a base DB."""
    try:
        with closing(_connect(data_dir, writable=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if index_available(connection):
                    connection.commit()
                    return {"status": "already_initialized", "schema_version": INDEX_VERSION}
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                for table, name, extra in _TRACKED:
                    update = "source_revision=source_revision+1"
                    if extra:
                        update += "," + extra
                    connection.execute(
                        f"CREATE TRIGGER environment_index_{name}_insert AFTER INSERT ON {table} "
                        "BEGIN INSERT OR IGNORE INTO environment_index_boots(device_id,boot_id) "
                        "VALUES(NEW.device_id,NEW.boot_id); "
                        f"UPDATE environment_index_boots SET {update} "
                        "WHERE device_id=NEW.device_id AND boot_id=NEW.boot_id; END"
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO environment_index_boots(device_id,boot_id) "
                        f"SELECT DISTINCT device_id,boot_id FROM {table}"
                    )
                connection.execute(
                    "UPDATE environment_index_boots AS b SET record_count="
                    "(SELECT COUNT(*) FROM environment_records r "
                    "WHERE r.device_id=b.device_id AND r.boot_id=b.boot_id), anchor_revision="
                    "(SELECT COUNT(*) FROM environment_time_anchors a "
                    "WHERE a.device_id=b.device_id AND a.boot_id=b.boot_id)"
                )
                connection.execute(
                    "UPDATE environment_index_boots AS b SET source_revision="
                    "record_count+anchor_revision+(SELECT COUNT(*) "
                    "FROM environment_device_status s "
                    "WHERE s.device_id=b.device_id AND s.boot_id=b.boot_id)"
                )
                connection.execute(
                    "INSERT INTO environment_index_schema VALUES(?,?,?)",
                    (INDEX_VERSION, TIME_MAPPING_ALGORITHM, _now()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        _secure(data_dir)
        return {"status": "initialized", "schema_version": INDEX_VERSION}
    except EnvironmentError:
        raise
    except Exception:
        raise EnvironmentError("environment_index_migration_failed") from None


def _build_mapper(connection, device_id: str, boot_id: str) -> BootTimeMapper:
    def anchors():
        cursor = connection.execute(
            "SELECT * FROM environment_time_anchors WHERE device_id=? AND boot_id=?",
            (device_id, boot_id),
        )
        for row in _rows(cursor):
            validate_environment_row("environment_time_anchors", row)
            yield TimeAnchor.model_validate_json(row["payload_json"])

    return build_time_mapper(anchors())


def _insert_projection(connection, mapper, version, where, parameters) -> None:
    """One SQL projection matches map_interval exactly, without loading a whole boot."""
    offset = mapper.to_dict()["_offset_us"]
    quality = canonical_json(list(mapper.quality))
    out_of_range = canonical_json([*mapper.quality, "utc_out_of_range"])
    # Wire records have positive <=60s windows. The pure mapping's UTC bounds
    # also prevent overflowing dates from being presented as plausible times.
    connection.execute(
        "WITH projected AS (SELECT r.*, mono_start_ms*1000+? AS start_us, "
        "mono_end_ms*1000+? AS end_us FROM environment_records r WHERE " + where + "), "
        "checked AS (SELECT *, (? IS NOT NULL AND ?<=start_us AND start_us<end_us "
        "AND end_us<=?) AS valid FROM projected) "
        "INSERT OR REPLACE INTO environment_time_index "
        "(device_id,boot_id,record_type,seq,profile_id,utc_start_us,utc_end_us,"
        "time_quality,time_uncertainty_ms,anchor_ids_json,quality_json,mapping_version) "
        "SELECT device_id,boot_id,record_type,seq,profile_id,"
        "CASE WHEN valid THEN start_us END, CASE WHEN valid THEN end_us END,"
        "CASE WHEN valid OR ? IS NULL THEN ? ELSE 'unknown' END,NULL,?,"
        "CASE WHEN valid OR ? IS NULL THEN ? ELSE ? END,? FROM checked",
        (
            offset,
            offset,
            *parameters,
            offset,
            MIN_UTC_US,
            MAX_UTC_US,
            offset,
            mapper.time_quality,
            canonical_json(list(mapper.anchor_ids)),
            offset,
            quality,
            out_of_range,
            version,
        ),
    )


def refresh_boot(
    connection: sqlite3.Connection,
    device_id: str,
    boot_id: str,
    *,
    force: bool = False,
    new_records: list[tuple[str, int]] | None = None,
    accepted_count: int | None = None,
) -> dict:
    """Refresh inside the caller's transaction; caller commits before sending ACK.

    Supplying the just-accepted keys avoids scanning historical records on each
    normal upload. If earlier unfinished work is detected, missing rows are
    backfilled instead. New compatible anchors only advance the index revision.
    """
    if not connection.in_transaction:
        raise EnvironmentError("environment_index_transaction_required")
    row = connection.execute(
        "SELECT * FROM environment_index_boots WHERE device_id=? AND boot_id=?",
        (device_id, boot_id),
    )
    boot = next(_rows(row), None)
    if boot is None:
        raise EnvironmentError("environment_index_boot_missing")
    if not force and boot["source_revision"] == boot["indexed_revision"]:
        return {"changed": False, "record_count": boot["record_count"]}
    try:
        if (
            not force
            and boot["mapping_json"] is not None
            and boot["anchor_revision"] == boot["indexed_anchor_revision"]
        ):
            mapper = BootTimeMapper.from_dict(json.loads(boot["mapping_json"]))
        else:
            mapper = _build_mapper(connection, device_id, boot_id)
        mapping_json = canonical_json(mapper.to_dict())
        replace_all = force or mapping_json != boot["mapping_json"]
        mapping_version = boot["mapping_version"] + int(replace_all)
        where, parameters = "r.device_id=? AND r.boot_id=?", [device_id, boot_id]
        if replace_all:
            connection.execute(
                "DELETE FROM environment_time_index WHERE device_id=? AND boot_id=?",
                (device_id, boot_id),
            )
        elif (
            new_records is not None
            and accepted_count == boot["source_revision"] - boot["indexed_revision"]
        ):
            if new_records:
                where += (
                    " AND ("
                    + " OR ".join("(r.record_type=? AND r.seq=?)" for _ in new_records)
                    + ")"
                )
                parameters.extend(value for key in new_records for value in key)
            else:
                where += " AND 0"
        else:
            where += (
                " AND NOT EXISTS(SELECT 1 FROM environment_time_index i WHERE "
                "i.device_id=r.device_id AND i.boot_id=r.boot_id "
                "AND i.record_type=r.record_type AND i.seq=r.seq)"
            )
        _insert_projection(connection, mapper, mapping_version, where, parameters)
        if mapper.time_quality == "unknown":
            unknown = boot["record_count"]
        elif mapper.time_quality == "conflict":
            unknown = 0
        else:
            unknown = connection.execute(
                "SELECT COUNT(*) FROM environment_time_index "
                "WHERE device_id=? AND boot_id=? AND time_quality='unknown'",
                (device_id, boot_id),
            ).fetchone()[0]
        conflicts = boot["record_count"] if mapper.time_quality == "conflict" else 0
        connection.execute(
            "UPDATE environment_index_boots SET indexed_revision=source_revision,"
            "indexed_anchor_revision=anchor_revision,mapping_version=?,mapping_json=?,"
            "indexed_at=?,unknown_count=?,conflict_count=? WHERE device_id=? AND boot_id=?",
            (mapping_version, mapping_json, _now(), unknown, conflicts, device_id, boot_id),
        )
        return {"changed": True, "record_count": boot["record_count"]}
    except EnvironmentError:
        raise
    except Exception:
        raise EnvironmentError("environment_index_refresh_failed") from None


def rebuild_index(data_dir: Path, *, force: bool = False) -> dict:
    """Explicit backfill/rebuild; one boot per transaction, no Garmin collection lock."""
    processed = 0
    try:
        with closing(_connect(data_dir, writable=True)) as connection:
            if not index_available(connection):
                raise EnvironmentError("environment_index_not_initialized")
            after = ("", "")
            while True:
                boots = connection.execute(
                    "SELECT device_id,boot_id FROM environment_index_boots "
                    "WHERE (device_id,boot_id)>(?,?) "
                    + ("" if force else "AND source_revision>indexed_revision ")
                    + "ORDER BY device_id,boot_id LIMIT 128",
                    after,
                ).fetchall()
                if not boots:
                    break
                for device_id, boot_id in boots:
                    with connection:
                        connection.execute("BEGIN IMMEDIATE")
                        result = refresh_boot(connection, device_id, boot_id, force=force)
                    processed += result["changed"]
                    after = device_id, boot_id
            counts = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(record_count),0),"
                "COALESCE(SUM(unknown_count),0),COALESCE(SUM(conflict_count),0),"
                "COALESCE(SUM(source_revision>indexed_revision),0) FROM environment_index_boots"
            ).fetchone()
        _secure(data_dir)
        return {
            "status": "ready" if counts[4] == 0 else "pending",
            "schema_version": INDEX_VERSION,
            "mapping_algorithm": TIME_MAPPING_ALGORITHM,
            "rebuilt_boots": processed,
            "boot_count": counts[0],
            "record_count": counts[1],
            "unknown_count": counts[2],
            "conflict_count": counts[3],
            "pending_boots": counts[4],
        }
    except EnvironmentError:
        raise
    except Exception:
        raise EnvironmentError("environment_index_rebuild_failed") from None


def validate_index(connection: sqlite3.Connection) -> None:
    """Backup/audit validation; no writes, also accepts safely marked pending boots."""
    try:
        if not index_available(connection):
            return
        for boot in _rows(connection.execute("SELECT * FROM environment_index_boots")):
            key = boot["device_id"], boot["boot_id"]
            raw_counts = [
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE device_id=? AND boot_id=?", key
                ).fetchone()[0]
                for table, _, _ in _TRACKED
            ]
            if (
                boot["source_revision"] != sum(raw_counts)
                or boot["record_count"] != raw_counts[0]
                or boot["anchor_revision"] != raw_counts[1]
            ):
                raise ValueError
            clean = boot["source_revision"] == boot["indexed_revision"]
            mapper = (
                BootTimeMapper.from_dict(json.loads(boot["mapping_json"]))
                if boot["mapping_json"] is not None
                else None
            )
            if clean and (
                mapper is None
                or mapper != _build_mapper(connection, *key)
                or boot["indexed_anchor_revision"] != boot["anchor_revision"]
            ):
                raise ValueError
            indexed = unknown = conflicts = 0
            cursor = connection.execute(
                "SELECT i.*,r.mono_start_ms,r.mono_end_ms,r.profile_id AS raw_profile "
                "FROM environment_time_index i JOIN environment_records r "
                "ON (i.device_id,i.boot_id,i.record_type,i.seq)="
                "(r.device_id,r.boot_id,r.record_type,r.seq) "
                "WHERE i.device_id=? AND i.boot_id=?",
                key,
            )
            for item in _rows(cursor):
                if mapper is None:
                    raise ValueError
                expected = mapper.map_interval(item["mono_start_ms"], item["mono_end_ms"])
                if (
                    item["utc_start_us"]
                    != (utc_to_us(expected.utc_start) if expected.utc_start else None)
                    or item["utc_end_us"]
                    != (utc_to_us(expected.utc_end) if expected.utc_end else None)
                    or item["time_quality"] != expected.time_quality
                    or item["time_uncertainty_ms"] != expected.time_uncertainty_ms
                    or item["anchor_ids_json"] != canonical_json(list(expected.anchor_ids))
                    or item["quality_json"] != canonical_json(list(expected.quality))
                    or item["mapping_version"] != boot["mapping_version"]
                    or item["profile_id"] != item["raw_profile"]
                ):
                    raise ValueError
                indexed += 1
                unknown += expected.time_quality == "unknown"
                conflicts += expected.time_quality == "conflict"
            if unknown != boot["unknown_count"] or conflicts != boot["conflict_count"]:
                raise ValueError
            if clean and indexed != boot["record_count"]:
                raise ValueError
        # The reference check also detects rows omitted by the joins above.
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError
        for table, _, _ in _TRACKED:
            if connection.execute(
                f"SELECT 1 FROM {table} r WHERE NOT EXISTS(SELECT 1 FROM environment_index_boots b "
                "WHERE b.device_id=r.device_id AND b.boot_id=r.boot_id) LIMIT 1"
            ).fetchone():
                raise ValueError
    except EnvironmentError:
        raise
    except Exception:
        raise EnvironmentError("environment_index_invalid") from None
