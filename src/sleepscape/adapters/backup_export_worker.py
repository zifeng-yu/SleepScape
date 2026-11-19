"""Private backup transport worker; also runs standalone under remote Python 3.

The caller selects retention candidates. This worker only copies/verifies bytes and
executes guarded deletions in its explicitly owned namespace. No shell commands.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

NAMESPACE = "sleepscape-managed-backups"
MAX_META = 128 * 1024
MAX_ENTRIES = 256
CHUNK = 1024 * 1024
FILES = {
    "manifest.json",
    "sleepscape.sqlite3",
    "model-runtime.json",
    "daily-runtime.json",
    "collection-cooldown.json",
}
ID = re.compile(r"[a-f0-9]{32}")
HASH = re.compile(r"[a-f0-9]{64}")


class ExportFailure(Exception):
    """Always carries a static code without source text or paths."""


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ExportFailure("export_metadata_invalid")
        value[key] = item
    return value


def decode(raw):
    if len(raw) > MAX_META:
        raise ExportFailure("export_metadata_invalid")
    try:
        return json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, TypeError, RecursionError):
        raise ExportFailure("export_metadata_invalid") from None


def encoded(value):
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_META:
        raise ExportFailure("export_metadata_invalid")
    return raw


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ExportFailure("export_destination_invalid")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ExportFailure("export_destination_invalid")
    return path


@contextmanager
def private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_nlink != 1
        ):
            raise ExportFailure("export_file_invalid")
        yield stream, info


def read_json(path):
    with private_file(path) as (stream, _):
        return decode(stream.read(MAX_META + 1))


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    temporary = path.parent / (".metadata-" + uuid4().hex)
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ExportFailure("export_metadata_invalid")
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _date(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ExportFailure("export_metadata_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ExportFailure("export_metadata_invalid") from None
    if result.tzinfo is None:
        raise ExportFailure("export_metadata_invalid")
    return result


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ExportFailure("export_metadata_invalid")
    return value


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ExportFailure("export_metadata_invalid")


def _descriptor(value):
    if not isinstance(value, dict) or set(value) != {"size", "sha256"}:
        raise ExportFailure("export_metadata_invalid")
    if type(value["size"]) is not int or not 0 <= value["size"] <= 1 << 50:
        raise ExportFailure("export_metadata_invalid")
    _hash(value["sha256"])


def validate_manifest(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "entries", "deleting"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["entries"], list)
        or len(value["entries"]) > MAX_ENTRIES
        or not isinstance(value["deleting"], list)
    ):
        raise ExportFailure("export_metadata_invalid")
    identities = set()
    for item in value["entries"]:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "created_at",
            "size_bytes",
            "manifest_sha256",
        }:
            raise ExportFailure("export_metadata_invalid")
        identity = _id(item["id"])
        if identity in identities:
            raise ExportFailure("export_metadata_invalid")
        identities.add(identity)
        _date(item["created_at"])
        _hash(item["manifest_sha256"])
        if type(item["size_bytes"]) is not int or item["size_bytes"] <= 0:
            raise ExportFailure("export_metadata_invalid")
    if len(set(value["deleting"])) != len(value["deleting"]):
        raise ExportFailure("export_metadata_invalid")
    if any(_id(item) not in identities for item in value["deleting"]):
        raise ExportFailure("export_metadata_invalid")
    return value


def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with private_file(path) as (stream, _):
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def inspect_bundle(path):
    private_directory(path)
    manifest = read_json(path / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ExportFailure("export_bundle_invalid")
    _id(manifest.get("backup_id"))
    _date(manifest.get("created_at"))
    records = manifest.get("files")
    if (
        not isinstance(records, dict)
        or "sleepscape.sqlite3" not in records
        or not set(records) <= FILES - {"manifest.json"}
        or set(p.name for p in path.iterdir()) != set(records) | {"manifest.json"}
    ):
        raise ExportFailure("export_bundle_invalid")
    for name, item in records.items():
        _descriptor(item)
        if digest_file(path / name) != item:
            raise ExportFailure("export_digest_mismatch")
    descriptors = dict(records)
    descriptors["manifest.json"] = digest_file(path / "manifest.json")
    return manifest, descriptors


@contextmanager
def destination(root):
    root = private_directory(root)
    namespace = root / NAMESPACE
    try:
        namespace.mkdir(mode=0o700)
        (namespace / "bundles").mkdir(mode=0o700)
        write_json(
            namespace / "manifest.json", {"schema_version": 1, "entries": [], "deleting": []}
        )
    except FileExistsError:
        pass
    private_directory(namespace)
    private_directory(namespace / "bundles")
    fd = os.open(namespace / ".export.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ExportFailure("export_destination_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExportFailure("export_busy") from None
        state = validate_manifest(read_json(namespace / "manifest.json"))
        yield namespace, state
    finally:
        os.close(fd)


def _checked_entry(namespace, item, *, allow_absent=False):
    path = namespace / "bundles" / item["id"]
    if allow_absent and not path.exists() and not path.is_symlink():
        return None
    manifest, descriptors = inspect_bundle(path)
    if (
        manifest["backup_id"] != item["id"]
        or manifest["created_at"] != item["created_at"]
        or descriptors["manifest.json"]["sha256"] != item["manifest_sha256"]
        or sum(v["size"] for v in descriptors.values()) != item["size_bytes"]
    ):
        raise ExportFailure("export_digest_mismatch")
    return path


def _recover_deletions(namespace, state):
    if not state["deleting"]:
        return
    pending = set(state["deleting"])
    keep = [row for row in state["entries"] if row["id"] not in pending]
    if not keep:
        raise ExportFailure("export_metadata_invalid")
    _checked_entry(namespace, max(keep, key=lambda row: (_date(row["created_at"]), row["id"])))
    for item in state["entries"]:
        if item["id"] in pending:
            _delete_checked(namespace / "bundles" / item["id"], item)
    fsync_directory(namespace / "bundles")
    state["entries"] = keep
    state["deleting"] = []
    write_json(namespace / "manifest.json", state)


def _delete_checked(path, entry):
    """Resume only a journaled deletion, verifying every remaining file before removal."""
    if not path.exists() and not path.is_symlink():
        return
    private_directory(path)
    names = {item.name for item in path.iterdir()}
    if not names:
        path.rmdir()
        return
    if "manifest.json" not in names:
        raise ExportFailure("export_bundle_invalid")
    manifest = read_json(path / "manifest.json")
    if (
        manifest.get("backup_id") != entry["id"]
        or digest_file(path / "manifest.json")["sha256"] != entry["manifest_sha256"]
    ):
        raise ExportFailure("export_digest_mismatch")
    descriptors = manifest.get("files")
    if not isinstance(descriptors, dict) or not set(descriptors) <= FILES - {"manifest.json"}:
        raise ExportFailure("export_bundle_invalid")
    if not names <= set(descriptors) | {"manifest.json"}:
        raise ExportFailure("export_bundle_invalid")
    for name in names - {"manifest.json"}:
        _descriptor(descriptors[name])
        if digest_file(path / name) != descriptors[name]:
            raise ExportFailure("export_digest_mismatch")
    for name in names - {"manifest.json"}:
        (path / name).unlink()
    fsync_directory(path)
    (path / "manifest.json").unlink()
    fsync_directory(path)
    path.rmdir()


def upload(request, source):
    identity = _id(request["backup_id"])
    _date(request["created_at"])
    descriptors = request["files"]
    if not isinstance(descriptors, dict) or not {"manifest.json", "sleepscape.sqlite3"} <= set(
        descriptors
    ):
        raise ExportFailure("export_metadata_invalid")
    if not set(descriptors) <= FILES:
        raise ExportFailure("export_metadata_invalid")
    for item in descriptors.values():
        _descriptor(item)
    entry = {
        "id": identity,
        "created_at": request["created_at"],
        "size_bytes": sum(item["size"] for item in descriptors.values()),
        "manifest_sha256": descriptors["manifest.json"]["sha256"],
    }
    with destination(request["destination"]) as (namespace, state):
        _recover_deletions(namespace, state)
        existing = next((item for item in state["entries"] if item["id"] == identity), None)
        if existing is not None:
            if existing != entry:
                raise ExportFailure("export_identity_conflict")
            _checked_entry(namespace, existing)
            _drain(source, descriptors)
            return _result(state, identity)
        if len(state["entries"]) >= MAX_ENTRIES:
            raise ExportFailure("export_manifest_capacity")
        final = namespace / "bundles" / identity
        if final.exists() or final.is_symlink():
            # Recover death after the immutable directory rename but before the
            # manifest commit only when every byte matches this explicit source.
            _checked_entry_directory(final, entry)
            _drain(source, descriptors)
            state["entries"].append(entry)
            write_json(namespace / "manifest.json", state)
            return _result(state, identity)
        temporary = namespace / (".staging-" + uuid4().hex)
        temporary.mkdir(mode=0o700)
        try:
            usage = shutil.disk_usage(namespace)
            if usage.free < entry["size_bytes"] + 64 * 1024 * 1024:
                raise ExportFailure("export_space_insufficient")
            for name, descriptor in descriptors.items():
                fd = os.open(temporary / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                digest = hashlib.sha256()
                remaining = descriptor["size"]
                with os.fdopen(fd, "wb") as target:
                    while remaining:
                        chunk = source.read(min(CHUNK, remaining))
                        if not chunk:
                            raise ExportFailure("export_transfer_incomplete")
                        target.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if digest.hexdigest() != descriptor["sha256"]:
                    raise ExportFailure("export_digest_mismatch")
            _checked_entry_directory(temporary, entry)
            fsync_directory(temporary)
            os.rename(temporary, final)
            fsync_directory(final.parent)
            state["entries"].append(entry)
            write_json(namespace / "manifest.json", state)
            return _result(state, identity)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def _checked_entry_directory(path, entry):
    manifest, descriptors = inspect_bundle(path)
    if (
        manifest["backup_id"] != entry["id"]
        or manifest["created_at"] != entry["created_at"]
        or descriptors["manifest.json"]["sha256"] != entry["manifest_sha256"]
        or sum(item["size"] for item in descriptors.values()) != entry["size_bytes"]
    ):
        raise ExportFailure("export_digest_mismatch")


def _drain(source, descriptors):
    """Consume and verify repeated input while retaining the already verified target."""
    for descriptor in descriptors.values():
        remaining = descriptor["size"]
        digest = hashlib.sha256()
        while remaining:
            chunk = source.read(min(CHUNK, remaining))
            if not chunk:
                raise ExportFailure("export_transfer_incomplete")
            digest.update(chunk)
            remaining -= len(chunk)
        if digest.hexdigest() != descriptor["sha256"]:
            raise ExportFailure("export_digest_mismatch")


def _result(state, identity):
    return {
        "status": "verified",
        "backup_id": identity,
        "entries": state["entries"],
        "manifest_hash": hashlib.sha256(encoded(state)).hexdigest(),
    }


def prune(request):
    protected = _id(request["backup_id"])
    candidates = request["delete"]
    if not isinstance(candidates, list) or len(candidates) != len(set(candidates)):
        raise ExportFailure("export_metadata_invalid")
    if any(_id(value) == protected for value in candidates):
        raise ExportFailure("export_retention_invalid")
    with destination(request["destination"]) as (namespace, state):
        _recover_deletions(namespace, state)
        if hashlib.sha256(encoded(state)).hexdigest() != request["manifest_hash"]:
            raise ExportFailure("export_manifest_changed")
        identities = {item["id"] for item in state["entries"]}
        if not set(candidates) <= identities or protected not in identities:
            raise ExportFailure("export_retention_invalid")
        newest = max(state["entries"], key=lambda row: (_date(row["created_at"]), row["id"]))
        if newest["id"] in candidates:
            raise ExportFailure("export_retention_invalid")
        # Validate every deletion and the protected valid copy before changing anything.
        for item in state["entries"]:
            if item["id"] in set(candidates) | {protected, newest["id"]}:
                _checked_entry(namespace, item)
        state["deleting"] = candidates
        write_json(namespace / "manifest.json", state)
        _recover_deletions(namespace, state)
        return _result(state, protected)


def dispatch(request, source):
    if request.get("operation") == "upload":
        return upload(request, source)
    if request.get("operation") == "prune":
        return prune(request)
    raise ExportFailure("export_operation_invalid")


def main():
    try:
        header = sys.stdin.buffer.readline(MAX_META + 1)
        request = decode(header)
        result = dispatch(request, sys.stdin.buffer)
    except ExportFailure as exc:
        result = {"status": "failed", "code": str(exc)}
    except Exception:
        result = {"status": "failed", "code": "export_io_failed"}
    sys.stdout.buffer.write(encoded(result) + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
