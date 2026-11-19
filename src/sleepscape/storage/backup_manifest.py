"""Private maintenance inventory and mutex. Reads never repair files or create directories."""

import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

STATE_FILE = "maintenance-state.json"
LOCK_FILE = ".maintenance.lock"
MAX_BYTES = 256 * 1024
MAX_BUNDLES = 256
MAX_RUNS = 31
ID = re.compile(r"^[a-f0-9]{32}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
ERROR_CODES = frozenset(
    {
        "maintenance_state_unavailable",
        "maintenance_already_running",
        "maintenance_failed",
        "maintenance_interrupted",
        "maintenance_clock_regressed",
        "maintenance_inventory_full",
        "maintenance_source_busy",
        "maintenance_source_unavailable",
        "maintenance_bundle_invalid",
        "maintenance_bundle_missing",
        "maintenance_bundle_conflict",
        "maintenance_legacy_limit",
        "maintenance_retention_failed",
        "maintenance_export_failed",
        "maintenance_capacity_blocked",
        "backup_source_unavailable",
        "backup_copy_timeout",
        "backup_schema_invalid",
        "backup_environment_schema_incomplete",
        "backup_environment_schema_unsupported",
        "backup_environment_index_without_facts",
        "backup_environment_index_schema_invalid",
        "backup_integrity_failed",
        "backup_foreign_keys_failed",
        "backup_raw_size_or_encoding_invalid",
        "backup_raw_hash_failed",
        "backup_raw_json_invalid",
        "backup_environment_payload_invalid",
        "backup_environment_index_invalid",
        "backup_analysis_schema_invalid",
        "backup_analysis_invalid",
        "backup_sleep_analysis_schema_invalid",
        "backup_sleep_analysis_invalid",
        "backup_restore_counts_mismatch",
    }
)


class MaintenanceError(Exception):
    def __init__(self, code="maintenance_failed"):
        self.code = code if code in ERROR_CODES else "maintenance_failed"
        super().__init__(self.code)


def safe_code(value):
    return value if isinstance(value, str) and value in ERROR_CODES else "maintenance_failed"


def directory(path, *, create=False):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise MaintenanceError("maintenance_state_unavailable")
    if not path.exists():
        if not create:
            return False
        path.mkdir(mode=0o700, parents=True)
    mode = path.stat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
        raise MaintenanceError("maintenance_state_unavailable")
    return True


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def read_json(path, limit=MAX_BYTES):
    """Read a bounded private regular file; missing is the only None outcome."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError
        return json.loads(raw, object_pairs_hook=_pairs)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        raise MaintenanceError("maintenance_state_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def write_json(path, value):
    path = Path(path)
    temporary = path.parent / f".{path.name}-{uuid4().hex}.tmp"
    try:
        directory(path.parent, create=True)
        if path.is_symlink():
            raise ValueError
        encoded = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > MAX_BYTES:
            raise ValueError
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        raise MaintenanceError("maintenance_state_unavailable") from None
    finally:
        temporary.unlink(missing_ok=True)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError
    return result


def _integer(value):
    if type(value) is not int or not 0 <= value <= 2**60:
        raise ValueError


def _validate(value):
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "bundles",
            "runs",
            "legacy",
            "offsite",
            "retention_over_budget",
            "export_fingerprint",
        }
        or value["schema_version"] != 1
        or type(value["schema_version"]) is not int
    ):
        raise ValueError
    bundles = value["bundles"]
    if not isinstance(bundles, list) or len(bundles) > MAX_BUNDLES:
        raise ValueError
    seen = set()
    for row in bundles:
        if set(row) != {"id", "created_at", "size_bytes", "manifest_sha256", "status"}:
            raise ValueError
        if not ID.fullmatch(row["id"]) or row["id"] in seen:
            raise ValueError
        seen.add(row["id"])
        _timestamp(row["created_at"])
        _integer(row["size_bytes"])
        if not HASH.fullmatch(row["manifest_sha256"]) or row["status"] not in {
            "verified",
            "deleting",
        }:
            raise ValueError
    if not isinstance(value["runs"], list) or len(value["runs"]) > MAX_RUNS:
        raise ValueError
    previous = None
    for row in value["runs"]:
        if set(row) != {
            "day",
            "status",
            "phase",
            "started_at",
            "finished_at",
            "error_code",
            "backup_id",
        }:
            raise ValueError
        start = _timestamp(row["started_at"])
        if previous and start < previous:
            raise ValueError
        previous = start
        from zoneinfo import ZoneInfo

        if row["day"] != start.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
            raise ValueError
        if row["status"] not in {
            "running",
            "completed",
            "partial",
            "failed",
            "interrupted",
            "blocked",
        }:
            raise ValueError
        if row["phase"] not in {"preparing", "backup", "export", "retention", "finished"}:
            raise ValueError
        if row["finished_at"] is not None and _timestamp(row["finished_at"]) < start:
            raise ValueError
        if row["status"] == "running" and row["finished_at"] is not None:
            raise ValueError
        if row["status"] != "running" and row["finished_at"] is None:
            raise ValueError
        if row["error_code"] is not None and row["error_code"] not in ERROR_CODES:
            raise ValueError
        if row["backup_id"] is not None and not ID.fullmatch(row["backup_id"]):
            raise ValueError
    if set(value["legacy"]) != {"count", "bytes", "verified_count", "invalid_count"}:
        raise ValueError
    for number in value["legacy"].values():
        _integer(number)
    if set(value["offsite"]) != {
        "status",
        "mode",
        "last_success_at",
        "error_code",
        "location_verified",
        "retained_count",
        "retained_bytes",
        "over_budget",
        "location_state",
        "transit_encrypted",
        "at_rest_encryption",
    }:
        raise ValueError
    offsite = value["offsite"]
    if offsite["status"] not in {"offsite_not_configured", "verified", "failed", "not_checked"}:
        raise ValueError
    if (
        offsite["mode"] not in {"none", "path", "ssh"}
        or type(offsite["location_verified"]) is not bool
    ):
        raise ValueError
    if offsite["last_success_at"] is not None:
        _timestamp(offsite["last_success_at"])
    if offsite["error_code"] is not None and offsite["error_code"] not in ERROR_CODES:
        raise ValueError
    for field in ("retained_count", "retained_bytes"):
        if offsite[field] is not None:
            _integer(offsite[field])
    if type(offsite["over_budget"]) is not bool or type(offsite["transit_encrypted"]) is not bool:
        raise ValueError
    if offsite["location_state"] not in {"not_configured", "location_unverified", "remote_host"}:
        raise ValueError
    if offsite["at_rest_encryption"] != "not_verified":
        raise ValueError
    if type(value["retention_over_budget"]) is not bool:
        raise ValueError
    if value["export_fingerprint"] is not None and not HASH.fullmatch(value["export_fingerprint"]):
        raise ValueError
    return value


def initial_state():
    return {
        "schema_version": 1,
        "bundles": [],
        "runs": [],
        "legacy": {"count": 0, "bytes": 0, "verified_count": 0, "invalid_count": 0},
        "offsite": {
            "status": "offsite_not_configured",
            "mode": "none",
            "last_success_at": None,
            "error_code": None,
            "location_verified": False,
            "retained_count": None,
            "retained_bytes": None,
            "over_budget": False,
            "location_state": "not_configured",
            "transit_encrypted": False,
            "at_rest_encryption": "not_verified",
        },
        "retention_over_budget": False,
        "export_fingerprint": None,
    }


def read_state(data_dir):
    try:
        if not directory(data_dir):
            return initial_state()
        value = read_json(Path(data_dir) / STATE_FILE)
        return _validate(initial_state() if value is None else value)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise MaintenanceError("maintenance_state_unavailable") from None


def write_state(data_dir, state):
    try:
        write_json(Path(data_dir) / STATE_FILE, _validate(state))
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise MaintenanceError("maintenance_state_unavailable") from None


@contextmanager
def maintenance_lock(data_dir):
    fd = None
    try:
        directory(data_dir, create=True)
        fd = os.open(Path(data_dir) / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MaintenanceError("maintenance_already_running") from None
        yield
    except (OSError, ValueError):
        raise MaintenanceError("maintenance_state_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def is_running(data_dir):
    fd = None
    try:
        if not directory(data_dir):
            return False
        try:
            fd = os.open(Path(data_dir) / LOCK_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    except (OSError, ValueError):
        raise MaintenanceError("maintenance_state_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)
