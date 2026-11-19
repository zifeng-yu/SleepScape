"""Bounded, read-only environment queries; original identities stay inside storage/services."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from sleepscape.domain.environment import DeviceStatus, MeasurementProfile, Record
from sleepscape.domain.environment_time import utc_to_us
from sleepscape.storage.environment import ENVIRONMENT_TABLES, EnvironmentError
from sleepscape.storage.environment_index import INDEX_TABLES, index_available
from sleepscape.storage.reader import StorageReadError, validate_window

_RECORD = TypeAdapter(Record)
_CLEAN = "b.source_revision=b.indexed_revision AND b.anchor_revision=b.indexed_anchor_revision"
_SELECT = """
SELECT t.*, r.mono_start_ms, r.mono_end_ms, r.received_at,
       r.payload_json, p.payload_json AS profile_json,
       b.mapping_version AS active_mapping_version, b.indexed_revision AS time_index_revision
FROM environment_time_index AS t INDEXED BY environment_time_index_utc
JOIN environment_index_boots AS b ON b.device_id=t.device_id AND b.boot_id=t.boot_id
JOIN environment_records AS r ON r.device_id=t.device_id AND r.boot_id=t.boot_id
 AND r.record_type=t.record_type AND r.seq=t.seq
JOIN environment_profiles AS p ON p.device_id=t.device_id AND p.profile_id=t.profile_id
WHERE t.utc_start_us>=? AND t.utc_start_us<? AND t.utc_end_us>?
 AND t.time_quality IN ('synced','estimated')
 AND b.source_revision=b.indexed_revision AND b.anchor_revision=b.indexed_anchor_revision
"""
_ORDER = " ORDER BY t.utc_start_us,t.utc_end_us,t.device_id,t.boot_id,t.record_type,t.seq LIMIT ?"
WINDOW_SQL = _SELECT + _ORDER


def instant_us(value: datetime) -> int:
    """Re-export the shared domain conversion for index query callers."""
    return utc_to_us(value)


def _json_list(value: str) -> list[str]:
    result = json.loads(value)
    if not isinstance(result, list) or any(not isinstance(item, str) for item in result):
        raise ValueError("invalid_index_list")
    return result


class EnvironmentReader:
    """One snapshot per request. No migration, rebuilding, creating, or implicit collection."""

    def __init__(self, data_dir: Path, *, connection: sqlite3.Connection | None = None):
        self._owns_connection = connection is None
        self.path = Path(data_dir) / "sleepscape.sqlite3"
        self.connection: sqlite3.Connection | None = connection
        self.state = "not_initialized"
        self.algorithm: str | None = None
        self.index_version: int | None = None

    def __enter__(self):
        try:
            if self.path.is_symlink() or self.path.parent.is_symlink():
                raise StorageReadError("environment_storage_unavailable")
            if not self.path.exists():
                return self
            if not self.path.is_file():
                raise StorageReadError("environment_storage_unavailable")
            if self._owns_connection:
                self.connection = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3
                )
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("PRAGMA query_only=ON")
                self.connection.execute("BEGIN")
            tables = {
                row[0]
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            present = tables & set(ENVIRONMENT_TABLES)
            if not present:
                if tables & set(INDEX_TABLES):
                    raise StorageReadError("environment_schema_incomplete")
                return self
            if present != set(ENVIRONMENT_TABLES):
                raise StorageReadError("environment_schema_incomplete")
            if [row[0] for row in self._query("SELECT version FROM environment_schema")] != [1]:
                raise StorageReadError("environment_schema_unsupported")
            if not index_available(self.connection):
                self.state = "index_not_initialized"
                return self
            versions = self._query("SELECT version,algorithm FROM environment_index_schema")
            self.index_version, self.algorithm = versions[0]
            self.state = "available"
            return self
        except (sqlite3.Error, OSError, ValueError, TypeError, EnvironmentError):
            self.close()
            raise StorageReadError("environment_storage_unavailable") from None
        except Exception:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.connection is not None:
            if self._owns_connection:
                self.connection.close()
            self.connection = None

    def _query(self, sql: str, values=()):
        if self.connection is None:
            return []
        try:
            return self.connection.execute(sql, values).fetchall()
        except sqlite3.Error:
            raise StorageReadError("environment_storage_unavailable") from None

    def time_summary(self) -> dict:
        """Boot metadata only: these counts describe all history, never a requested night."""
        result = {
            "scope": "all_history_not_window",
            "evaluated": self.state == "available",
            "boot_count": None,
            "source_record_count": None,
            "current_index_record_count": None,
            "stale_boot_count": None,
            "stale_source_record_count": None,
            "unknown_record_count": None,
            "conflict_record_count": None,
        }
        if self.state != "available":
            return result
        row = self._query(
            f"SELECT COUNT(*),COALESCE(SUM(record_count),0),"
            f"COALESCE(SUM(CASE WHEN {_CLEAN} THEN record_count ELSE 0 END),0),"
            f"COALESCE(SUM(NOT ({_CLEAN})),0),"
            f"COALESCE(SUM(CASE WHEN NOT ({_CLEAN}) THEN record_count ELSE 0 END),0),"
            f"COALESCE(SUM(CASE WHEN {_CLEAN} THEN unknown_count ELSE 0 END),0),"
            f"COALESCE(SUM(CASE WHEN {_CLEAN} THEN conflict_count ELSE 0 END),0) "
            "FROM environment_index_boots AS b"
        )[0]
        result.update(zip(tuple(result)[2:], row, strict=True))
        return result

    def timeline(
        self, start: datetime, end: datetime, *, limit: int = 10000, device_id: str | None = None
    ) -> dict:
        validate_window(start, end)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("environment_limit_requires_1_to_10000")
        result = {
            "state": self.state,
            "records": [],
            "truncated": False,
            "limit": limit,
            "algorithm": self.algorithm,
            "index_version": self.index_version,
            "global_time_state": self.time_summary(),
        }
        if self.state != "available":
            return result
        values: tuple = (instant_us(start) - 60_000_000, instant_us(end), instant_us(start))
        sql = WINDOW_SQL
        if device_id is not None:
            sql = (
                _SELECT.replace(
                    "INDEXED BY environment_time_index_utc",
                    "INDEXED BY environment_time_index_device_utc",
                )
                + " AND t.device_id=?"
                + _ORDER
            )
            values += (device_id,)
        rows = self._query(sql, (*values, limit + 1))
        result["truncated"] = len(rows) > limit
        profiles: dict[tuple[str, str], dict] = {}
        try:
            for row in rows[:limit]:
                item = dict(row)
                key = (item["device_id"], item["profile_id"])
                profile_json = item.pop("profile_json")
                if key not in profiles:
                    profiles[key] = MeasurementProfile.model_validate_json(profile_json).model_dump(
                        mode="json"
                    )
                item["profile"] = profiles[key]
                record = _RECORD.validate_json(item.pop("payload_json"))
                if (
                    record.seq != item["seq"]
                    or record.record_type != item["record_type"]
                    or record.measurement_profile_id != item["profile_id"]
                    or record.mono_start_ms != item["mono_start_ms"]
                    or record.mono_end_ms != item["mono_end_ms"]
                    or item["utc_end_us"] - item["utc_start_us"]
                    != (record.mono_end_ms - record.mono_start_ms) * 1000
                    or item["mapping_version"] != item["active_mapping_version"]
                ):
                    raise ValueError("environment_index_identity_mismatch")
                item["record"] = record.model_dump(mode="json")
                item["anchor_ids"] = _json_list(item.pop("anchor_ids_json"))
                item["time_flags"] = _json_list(item.pop("quality_json"))
                utc_to_us(item["received_at"])
                result["records"].append(item)
        except (ValueError, TypeError, KeyError):
            raise StorageReadError("environment_data_invalid") from None
        return result

    def _last_capture(self, device_id: str) -> tuple[dict | None, str]:
        # Bound metadata work before seeking one minute per clean boot. Never walk
        # every stale record in a year-long index merely to find the newest good one.
        boots = self._query(
            "SELECT boot_id,mapping_version FROM environment_index_boots b WHERE device_id=? "
            f"AND {_CLEAN} ORDER BY boot_id LIMIT 513",
            (device_id,),
        )
        if len(boots) > 512:
            return None, "boot_limit_exceeded"
        latest = None
        for boot in boots:
            rows = self._query(
                "SELECT t.boot_id,t.profile_id,t.seq,t.utc_start_us,t.utc_end_us,t.time_quality,"
                "t.mapping_version,r.received_at,r.payload_json,p.payload_json AS profile_json "
                "FROM environment_time_index t "
                "INDEXED BY environment_time_index_boot_utc "
                "JOIN environment_records r ON r.device_id=t.device_id AND r.boot_id=t.boot_id "
                "AND r.record_type=t.record_type AND r.seq=t.seq "
                "JOIN environment_profiles p ON p.device_id=t.device_id "
                "AND p.profile_id=t.profile_id "
                "WHERE t.device_id=? AND t.boot_id=? AND t.record_type='minute' "
                "AND t.utc_start_us IS NOT NULL "
                "ORDER BY t.utc_start_us DESC LIMIT 1",
                (device_id, boot[0]),
            )
            if rows and rows[0]["mapping_version"] != boot["mapping_version"]:
                raise StorageReadError("environment_data_invalid")
            if rows and (latest is None or rows[0]["utc_end_us"] > latest["utc_end_us"]):
                latest = dict(rows[0])
        if latest is not None:
            try:
                latest["profile"] = MeasurementProfile.model_validate_json(
                    latest.pop("profile_json")
                ).model_dump(mode="json")
                latest["record"] = _RECORD.validate_json(latest.pop("payload_json")).model_dump(
                    mode="json"
                )
                if (
                    latest["record"]["record_type"] != "minute"
                    or latest["record"]["seq"] != latest["seq"]
                    or latest["record"]["measurement_profile_id"] != latest["profile_id"]
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise StorageReadError("environment_data_invalid") from None
        return latest, "available" if latest else "no_mapped_capture"

    def status(self, *, device_limit: int = 100) -> dict:
        if type(device_limit) is not int or not 1 <= device_limit <= 100:
            raise ValueError("environment_device_limit_requires_1_to_100")
        result = {
            "state": self.state,
            "algorithm": self.algorithm,
            "index_version": self.index_version,
            "global_time_state": self.time_summary(),
            "devices": [],
            "devices_truncated": False,
        }
        if self.state == "not_initialized":
            return result
        devices = self._query(
            "SELECT device_id,enabled FROM environment_devices ORDER BY device_id LIMIT ?",
            (device_limit + 1,),
        )
        result["devices_truncated"] = len(devices) > device_limit
        for device in devices[:device_limit]:
            identity = device["device_id"]
            if self.state != "available":
                result["devices"].append(
                    {
                        "device_id": identity,
                        "enabled": bool(device["enabled"]),
                        "last_received_at": None,
                        "last_received_snapshot": None,
                        "last_mapped_capture": None,
                        "capture_state": "not_indexed",
                        "receive_state": "not_indexed",
                    }
                )
                continue
            received = self._query(
                "SELECT received_at FROM environment_ingest_batches "
                "INDEXED BY environment_ingest_device_received WHERE device_id=? "
                "ORDER BY received_at DESC,id DESC LIMIT 1",
                (identity,),
            )
            latest = self._query(
                "SELECT boot_id,status_seq,monotonic_ms,received_at,payload_json "
                "FROM environment_device_status INDEXED BY environment_status_device_received "
                "WHERE device_id=? "
                "ORDER BY received_at DESC,status_seq DESC LIMIT 1",
                (identity,),
            )
            snapshot = None
            if latest:
                try:
                    snapshot = dict(latest[0])
                    snapshot["status"] = DeviceStatus.model_validate_json(
                        snapshot.pop("payload_json")
                    ).model_dump(mode="json")
                except (ValueError, TypeError):
                    raise StorageReadError("environment_data_invalid") from None
            mapped, capture_state = self._last_capture(identity)
            result["devices"].append(
                {
                    "device_id": identity,
                    "enabled": bool(device["enabled"]),
                    "last_received_at": received[0][0] if received else None,
                    "last_received_snapshot": snapshot,
                    "last_mapped_capture": mapped,
                    "capture_state": capture_state,
                    "receive_state": "available",
                }
            )
        return result
