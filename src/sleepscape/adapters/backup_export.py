"""Bounded verified backup export through a mounted path or strict OpenSSH transport."""

import re
import shlex
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from sleepscape.adapters import backup_export_worker as worker
from sleepscape.domain.retention import select_offsite

ERROR_CODES = frozenset(
    {
        "export_configuration_invalid",
        "export_destination_invalid",
        "export_file_invalid",
        "export_metadata_invalid",
        "export_bundle_invalid",
        "export_digest_mismatch",
        "export_busy",
        "export_identity_conflict",
        "export_transfer_incomplete",
        "export_manifest_capacity",
        "export_unregistered_destination",
        "export_space_insufficient",
        "export_retention_invalid",
        "export_manifest_changed",
        "export_operation_invalid",
        "export_io_failed",
        "export_timeout",
        "export_transport_failed",
        "export_response_invalid",
    }
)


class BackupExportError(Exception):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "export_io_failed"
        super().__init__(self.code)


def _regular_private(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise BackupExportError("export_configuration_invalid")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise BackupExportError("export_configuration_invalid")
    worker.private_directory(path.parent)
    return path


def configuration(settings):
    """Validate configuration metadata only; never read key contents or contact a target."""
    mode = settings.maintenance_export_mode
    if mode == "disabled":
        return {"mode": mode}
    root = settings.maintenance_export_path
    if root is None or not Path(root).is_absolute() or "\x00" in str(root):
        raise BackupExportError("export_configuration_invalid")
    if not 1 <= settings.maintenance_export_timeout_seconds <= 3600:
        raise BackupExportError("export_configuration_invalid")
    if settings.maintenance_export_max_bytes < 1:
        raise BackupExportError("export_configuration_invalid")
    if mode == "path":
        worker.private_directory(root)
        local_data = Path(settings.data_dir).absolute()
        if root == local_data or local_data in root.parents:
            raise BackupExportError("export_configuration_invalid")
        return {"mode": mode, "root": str(root)}
    if mode != "ssh":
        raise BackupExportError("export_configuration_invalid")
    host = settings.maintenance_export_host
    user = settings.maintenance_export_user
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host):
        raise BackupExportError("export_configuration_invalid")
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
        raise BackupExportError("export_configuration_invalid")
    if (
        type(settings.maintenance_export_port) is not int
        or not 1 <= settings.maintenance_export_port <= 65535
    ):
        raise BackupExportError("export_configuration_invalid")
    if (
        not settings.maintenance_export_identity_file
        or not settings.maintenance_export_known_hosts_file
    ):
        raise BackupExportError("export_configuration_invalid")
    identity = _regular_private(settings.maintenance_export_identity_file)
    known_hosts = _regular_private(settings.maintenance_export_known_hosts_file)
    return {
        "mode": mode,
        "root": str(root),
        "host": host,
        "user": user,
        "port": settings.maintenance_export_port,
        "identity": str(identity),
        "known_hosts": str(known_hosts),
    }


def _command(config):
    script = Path(worker.__file__).with_suffix(".py")
    if config["mode"] == "path":
        return [sys.executable, "-I", "-B", str(script)]
    # SSH uses a remote shell for its command argument. Only fixed trusted source
    # enters this quoted command; destination paths and metadata travel via stdin.
    remote_command = "python3 -I -B -c " + shlex.quote(script.read_text(encoding="utf-8"))
    return [
        "ssh",
        "-F",
        "/dev/null",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UserKnownHostsFile=" + config["known_hosts"],
        "-i",
        config["identity"],
        "-p",
        str(config["port"]),
        "-l",
        config["user"],
        "--",
        config["host"],
        remote_command,
    ]


def _invoke(config, request, bundle_path, timeout):
    if timeout <= 0:
        raise BackupExportError("export_timeout")
    header = worker.encoded(request) + b"\n"
    failures = []
    process = None
    writer = None
    try:
        process = subprocess.Popen(
            _command(config),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

        def feed():
            try:
                process.stdin.write(header)
                for name, descriptor in request.get("files", {}).items():
                    with worker.private_file(bundle_path / name) as (source, info):
                        if info.st_size != descriptor["size"]:
                            raise BackupExportError("export_digest_mismatch")
                        remaining = info.st_size
                        while remaining:
                            chunk = source.read(min(worker.CHUNK, remaining))
                            if not chunk:
                                raise BackupExportError("export_transfer_incomplete")
                            process.stdin.write(chunk)
                            remaining -= len(chunk)
                process.stdin.close()
            except Exception:
                failures.append(True)
                try:
                    process.stdin.close()
                except OSError:
                    pass

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BackupExportError("export_timeout") from None
        writer.join(timeout=1)
        raw = process.stdout.read(worker.MAX_META + 1)
        if process.returncode != 0:
            raise BackupExportError("export_transport_failed")
        response = worker.decode(raw)
        if not isinstance(response, dict):
            raise BackupExportError("export_response_invalid")
        if response.get("status") == "failed":
            raise BackupExportError(response.get("code"))
        if failures or writer.is_alive():
            raise BackupExportError("export_transfer_incomplete")
        if set(response) != {"status", "backup_id", "entries", "manifest_hash"}:
            raise BackupExportError("export_response_invalid")
        if response["status"] != "verified" or response["backup_id"] != request["backup_id"]:
            raise BackupExportError("export_response_invalid")
        state = worker.validate_manifest(
            {
                "schema_version": 1,
                "entries": response["entries"],
                "deleting": [],
            }
        )
        if (
            __import__("hashlib").sha256(worker.encoded(state)).hexdigest()
            != response["manifest_hash"]
        ):
            raise BackupExportError("export_response_invalid")
        return response
    except BackupExportError:
        raise
    except (OSError, ValueError, TypeError, KeyError, worker.ExportFailure):
        raise BackupExportError("export_transport_failed") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if writer is not None:
                writer.join(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


def export_bundle(settings, bundle_path, *, now=None):
    """Publish and verify first, then apply the pure retention plan to known artifacts."""
    del now  # Bundle creation time, not export/retry time, determines retention buckets.
    started = time.monotonic()
    try:
        config = configuration(settings)
        if config["mode"] == "disabled":
            return {"status": "offsite_not_configured", "code": None}
        bundle_path = Path(bundle_path).absolute()
        manifest, descriptors = worker.inspect_bundle(bundle_path)
        if config["mode"] == "path":
            target = Path(config["root"])
            if target == bundle_path or bundle_path in target.parents:
                raise BackupExportError("export_configuration_invalid")
        identity = manifest["backup_id"]
        deadline = started + settings.maintenance_export_timeout_seconds
        request = {
            "operation": "upload",
            "destination": config["root"],
            "backup_id": identity,
            "created_at": manifest["created_at"],
            "files": descriptors,
        }
        response = _invoke(config, request, bundle_path, deadline - time.monotonic())
        entries = response["entries"]
        plan = select_offsite(
            entries,
            daily_count=settings.maintenance_export_daily_keep,
            weekly_count=settings.maintenance_export_weekly_keep,
            max_bytes=settings.maintenance_export_max_bytes,
        )
        # An explicit retry of an older backup is never allowed to delete that same
        # just-verified source in this run. A future newest export can retire it.
        deleting = [key for key in plan["delete"] if key != identity]
        if deleting:
            response = _invoke(
                config,
                {
                    "operation": "prune",
                    "destination": config["root"],
                    "backup_id": identity,
                    "delete": deleting,
                    "manifest_hash": response["manifest_hash"],
                },
                bundle_path,
                deadline - time.monotonic(),
            )
        kept = response["entries"]
        return {
            "status": "verified",
            "code": None,
            "transport": config["mode"],
            "location_state": "remote_host" if config["mode"] == "ssh" else "location_unverified",
            "transit_encrypted": config["mode"] == "ssh",
            "at_rest_encryption": "not_verified",
            "retained_count": len(kept),
            "retained_bytes": sum(row["size_bytes"] for row in kept),
            "deleted_count": len(deleting),
            "over_budget": sum(row["size_bytes"] for row in kept)
            > settings.maintenance_export_max_bytes,
        }
    except BackupExportError:
        raise
    except worker.ExportFailure as exc:
        raise BackupExportError(str(exc)) from None
    except Exception:
        raise BackupExportError("export_io_failed") from None
