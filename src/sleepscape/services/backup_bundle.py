"""Verified SQLite plus runtime-state snapshots; no in-place restore or external calls."""

import hashlib
import os
import stat
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sleepscape.models.base import ModelError
from sleepscape.services.backup import BackupError, _copy, _counts, _readonly, _validate
from sleepscape.storage import daily_runtime, model_runtime
from sleepscape.storage.backup_manifest import (
    HASH,
    ID,
    MaintenanceError,
    directory,
    fsync_directory,
    read_json,
    write_json,
)
from sleepscape.storage.usage import CapacityError, require_capacity

RUNTIME_FILES = ("model-runtime.json", "daily-runtime.json", "collection-cooldown.json")
DATABASE = "sleepscape.sqlite3"
MANIFEST = "manifest.json"
ALLOWED_FILES = frozenset((DATABASE, MANIFEST, *RUNTIME_FILES))


def file_digest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise MaintenanceError("maintenance_bundle_invalid")
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        return {"sha256": digest.hexdigest(), "size": info.st_size}


def bundle_directory(data_dir, backup_id):
    if not isinstance(backup_id, str) or not ID.fullmatch(backup_id):
        raise MaintenanceError("maintenance_bundle_invalid")
    return Path(data_dir) / "backups" / "managed" / backup_id


def _manifest(path):
    directory(path)
    value = read_json(path / MANIFEST, 128 * 1024)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "backup_id",
        "created_at",
        "files",
        "counts",
        "runtime_presence",
    }:
        raise ValueError
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError
    if not ID.fullmatch(value["backup_id"]):
        raise ValueError
    from datetime import datetime

    if datetime.fromisoformat(value["created_at"]).tzinfo is None:
        raise ValueError
    files = value["files"]
    if (
        not isinstance(files, dict)
        or DATABASE not in files
        or not set(files) <= ALLOWED_FILES - {MANIFEST}
    ):
        raise ValueError
    if set(value["runtime_presence"]) != set(RUNTIME_FILES):
        raise ValueError
    for name in RUNTIME_FILES:
        if type(value["runtime_presence"][name]) is not bool:
            raise ValueError
        if value["runtime_presence"][name] != (name in files):
            raise ValueError
    for item in files.values():
        if set(item) != {"sha256", "size"} or not HASH.fullmatch(item["sha256"]):
            raise ValueError
        if type(item["size"]) is not int or not 0 <= item["size"] <= 2**60:
            raise ValueError
    if not isinstance(value["counts"], dict) or not value["counts"]:
        raise ValueError
    for name, count in value["counts"].items():
        if not isinstance(name, str) or type(count) is not int or count < 0:
            raise ValueError
    return value


def _runtime_valid(path, connection):
    model_runtime.read_state(path)
    daily = daily_runtime.read_state(path)
    daily_runtime.cooldown(path)
    for row in daily["days"]:
        if row["run_id"] is None:
            continue
        stored = connection.execute(
            "SELECT status FROM runs WHERE id=?", (row["run_id"],)
        ).fetchone()
        if stored is None or (row["status"] == "completed" and stored[0] != "completed"):
            raise MaintenanceError("maintenance_bundle_invalid")


def validate_bundle(path, *, restore=False):
    """Explicit expensive check. Unknown files, altered hashes or inconsistent ledgers fail."""
    path = Path(path)
    try:
        manifest = _manifest(path)
        if {entry.name for entry in path.iterdir()} != set(manifest["files"]) | {MANIFEST}:
            raise ValueError
        for name, expected in manifest["files"].items():
            if file_digest(path / name) != expected:
                raise ValueError
        with closing(_readonly(path / DATABASE)) as connection:
            if _validate(connection) != manifest["counts"]:
                raise ValueError
            _runtime_valid(path, connection)
        if restore:
            with TemporaryDirectory(
                prefix=".sleepscape-bundle-restore-", dir=path.parent
            ) as temporary:
                target = Path(temporary).resolve()
                target.chmod(0o700)
                with closing(_readonly(path / DATABASE)) as source:
                    source.execute("BEGIN")
                    _copy(source, target / DATABASE)
                for name in RUNTIME_FILES:
                    if name in manifest["files"]:
                        _copy_runtime(path / name, target / name)
                with closing(_readonly(target / DATABASE)) as restored:
                    if _validate(restored) != manifest["counts"]:
                        raise ValueError
                    _runtime_valid(target, restored)
                for name in RUNTIME_FILES:
                    if (
                        name in manifest["files"]
                        and file_digest(target / name) != manifest["files"][name]
                    ):
                        raise ValueError
        return manifest
    except (BackupError, ModelError, daily_runtime.DailyRuntimeError, MaintenanceError):
        raise MaintenanceError("maintenance_bundle_invalid") from None
    except Exception:
        raise MaintenanceError("maintenance_bundle_invalid") from None


def _copy_runtime(source, target):
    limit = (
        model_runtime.MAX_STATE_BYTES
        if source.name == model_runtime.STATE_FILE
        else daily_runtime.MAX_BYTES
    )
    data = read_json(source, limit)
    if data is None:
        raise MaintenanceError("maintenance_bundle_invalid")
    # Preserve exact bytes, including state budgets, rather than reconstructing default state.
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise MaintenanceError("maintenance_bundle_invalid")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise MaintenanceError("maintenance_bundle_invalid")
    out = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(out, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def create_bundle(settings, backup_id, now):
    """Capture locks only protect the SQLite/ledger snapshot, never validation or export."""
    target = bundle_directory(settings.data_dir, backup_id)
    temporary = target.parent / f".{backup_id}.partial"
    created = False
    try:
        directory(settings.data_dir)
        directory(target.parent.parent, create=True)
        directory(target.parent, create=True)
        if target.exists() or target.is_symlink() or temporary.exists() or temporary.is_symlink():
            raise MaintenanceError("maintenance_bundle_conflict")
        temporary.mkdir(mode=0o700)
        created = True
        with (
            daily_runtime.collection_mutex(settings.data_dir),
            model_runtime.session(settings.data_dir),
        ):
            with closing(_readonly(settings.data_dir / DATABASE)) as source:
                source.execute("BEGIN")
                expected = _counts(source)
                snapshot_bytes = (
                    source.execute("PRAGMA page_count").fetchone()[0]
                    * source.execute("PRAGMA page_size").fetchone()[0]
                )
                runtime_bytes = model_runtime.MAX_STATE_BYTES + 2 * daily_runtime.MAX_BYTES
                require_capacity(
                    settings.data_dir, "backup", (snapshot_bytes + runtime_bytes) * 3 + 1024**2
                )
                _runtime_valid(settings.data_dir, source)
                _copy(source, temporary / DATABASE)
                present = {}
                for name in RUNTIME_FILES:
                    present[name] = (settings.data_dir / name).exists()
                    if present[name]:
                        _copy_runtime(settings.data_dir / name, temporary / name)
        manifest = {
            "schema_version": 1,
            "backup_id": backup_id,
            "created_at": now.isoformat(),
            "files": {path.name: file_digest(path) for path in temporary.iterdir()},
            "counts": expected,
            "runtime_presence": present,
        }
        write_json(temporary / MANIFEST, manifest)
        validate_bundle(temporary, restore=True)
        os.rename(temporary, target)
        fsync_directory(target.parent)
        return inventory_entry(target, manifest)
    except (daily_runtime.DailyRuntimeError, ModelError) as exc:
        if exc.code in {"collection_already_running", "model_busy"}:
            raise MaintenanceError("maintenance_source_busy") from None
        raise MaintenanceError("maintenance_bundle_invalid") from None
    except BackupError as exc:
        raise MaintenanceError(str(exc)) from None
    except CapacityError:
        raise MaintenanceError("maintenance_capacity_blocked") from None
    except MaintenanceError:
        raise
    except Exception:
        raise MaintenanceError("maintenance_failed") from None
    finally:
        if created and temporary.exists() and not temporary.is_symlink():
            _remove_partial(temporary)


def _remove_partial(path):
    """Only known files produced by this snapshot, never recursively delete arbitrary contents."""
    files = list(path.iterdir())
    if any(p.name not in ALLOWED_FILES or p.is_symlink() or not p.is_file() for p in files):
        raise MaintenanceError("maintenance_bundle_conflict")
    for item in files:
        item.unlink()
    path.rmdir()


def inventory_entry(path, manifest=None):
    manifest = manifest or validate_bundle(path)
    digest = file_digest(path / MANIFEST)
    return {
        "id": manifest["backup_id"],
        "created_at": manifest["created_at"],
        "size_bytes": sum(row["size"] for row in manifest["files"].values()) + digest["size"],
        "manifest_sha256": digest["sha256"],
        "status": "verified",
    }


def remove_registered_bundle(data_dir, entry):
    """Repeatable deletion of one marked entry; the immutable manifest is removed last."""
    path = bundle_directory(data_dir, entry["id"])
    if path.is_symlink():
        raise MaintenanceError("maintenance_bundle_conflict")
    if not path.exists():
        return
    directory(path)
    if not (path / MANIFEST).exists():
        if list(path.iterdir()):
            raise MaintenanceError("maintenance_bundle_conflict")
        path.rmdir()
        return
    try:
        manifest = _manifest(path)
        if (
            manifest["backup_id"] != entry["id"]
            or file_digest(path / MANIFEST)["sha256"] != entry["manifest_sha256"]
        ):
            raise ValueError
        existing = {p.name for p in path.iterdir()}
        if not existing <= set(manifest["files"]) | {MANIFEST}:
            raise ValueError
        for name in existing - {MANIFEST}:
            if file_digest(path / name) != manifest["files"][name]:
                raise ValueError
        for name in existing - {MANIFEST}:
            (path / name).unlink()
        (path / MANIFEST).unlink()
        path.rmdir()
        fsync_directory(path.parent)
    except Exception:
        raise MaintenanceError("maintenance_retention_failed") from None
