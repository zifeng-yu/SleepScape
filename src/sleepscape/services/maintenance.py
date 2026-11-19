"""Explicit, bounded maintenance: verified bundles, export, then controlled retention."""

import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sleepscape.domain.retention import select_local
from sleepscape.services.backup import BackupError, _readonly, _validate, source_size
from sleepscape.services.backup_bundle import (
    _remove_partial,
    bundle_directory,
    create_bundle,
    inventory_entry,
    remove_registered_bundle,
    validate_bundle,
)
from sleepscape.storage.backup_manifest import (
    MAX_BUNDLES,
    MAX_RUNS,
    MaintenanceError,
    directory,
    initial_state,
    is_running,
    maintenance_lock,
    read_state,
    safe_code,
    write_state,
)
from sleepscape.storage.usage import CapacityError, disk_snapshot, require_capacity

ZONE = ZoneInfo("Asia/Shanghai")
LEGACY_LIMIT = 256


def _now(now=None):
    result = now if now is not None else datetime.now(UTC)
    if result.tzinfo is None:
        raise MaintenanceError("maintenance_clock_regressed")
    return result.astimezone(UTC)


def _schedule(now):
    local = now.astimezone(ZONE)
    planned = datetime.combine(local.date(), time(4), ZONE)
    if planned <= local:
        planned += timedelta(days=1)
    return {
        "time": "04:00",
        "timezone": "Asia/Shanghai",
        "installation_state": "unknown",
        "next_planned_at": planned.isoformat(),
    }


def _public_run(row):
    return {key: value for key, value in row.items() if key != "backup_id"}


def _export_fingerprint(settings):
    fields = (
        "mode",
        "path",
        "host",
        "user",
        "port",
        "identity_file",
        "known_hosts_file",
        "max_bytes",
        "daily_keep",
        "weekly_keep",
    )
    raw = [str(getattr(settings, "maintenance_export_" + field)) for field in fields]
    return hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode()).hexdigest()


def maintenance_status(settings, *, now=None):
    """Bounded metadata and statvfs only: no backup scans, hashing, repair or network."""
    clock = _now(now)
    result = {
        "state": "not_run",
        "schedule": _schedule(clock),
        "latest": None,
        "error_code": None,
        "disk": disk_snapshot(settings.data_dir),
    }
    state = initial_state()
    try:
        state = read_state(settings.data_dir)
        result["state"] = "available" if state["runs"] else "not_run"
        if state["runs"]:
            latest = _public_run(state["runs"][-1])
            if datetime.fromisoformat(latest["started_at"]) > clock:
                raise MaintenanceError("maintenance_clock_regressed")
            if latest["status"] == "running" and not is_running(settings.data_dir):
                latest.update(
                    status="interrupted", phase="finished", error_code="maintenance_interrupted"
                )
            result["latest"] = latest
    except MaintenanceError as exc:
        result.update(state="unavailable", error_code=exc.code, latest=None)
    verified = [row for row in state["bundles"] if row["status"] == "verified"]
    result["backups"] = {
        "verified_count": len(verified),
        "total_bytes": sum(row["size_bytes"] for row in verified),
        "latest_verified_at": max((row["created_at"] for row in verified), default=None),
        "retention_over_budget": state["retention_over_budget"],
        "legacy_count": state["legacy"]["count"],
        "legacy_bytes": state["legacy"]["bytes"],
    }
    if result["state"] == "unavailable":
        result["backups"].update(
            verified_count=None,
            total_bytes=None,
            latest_verified_at=None,
            legacy_count=None,
            legacy_bytes=None,
        )
    offsite = dict(state["offsite"])
    mode = (
        "none"
        if settings.maintenance_export_mode == "disabled"
        else settings.maintenance_export_mode
    )
    if offsite["mode"] != mode or (
        mode != "none" and state["export_fingerprint"] != _export_fingerprint(settings)
    ):
        offsite = initial_state()["offsite"]
        offsite.update(
            status="offsite_not_configured" if mode == "none" else "not_checked", mode=mode
        )
    result["offsite"] = offsite
    return result


def plan_maintenance(settings, *, now=None):
    status = maintenance_status(settings, now=now)
    return {
        "status": "planned",
        "schedule": status["schedule"],
        "disk": status["disk"],
        "retention": {
            "keep_count": settings.maintenance_keep_count,
            "max_bytes": settings.maintenance_max_bytes,
        },
        "offsite": status["offsite"],
        "runtime_state": status["state"],
        "note": "read_only_no_backup_export_or_cleanup",
    }


def _legacy_inventory(settings):
    location = settings.data_dir / "backups"
    result = {"count": 0, "bytes": 0, "verified_count": 0, "invalid_count": 0}
    if not directory(location):
        return result
    for index, path in enumerate(location.iterdir()):
        if index >= LEGACY_LIMIT:
            raise MaintenanceError("maintenance_legacy_limit")
        if path.name == "managed":
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".sqlite3":
            # Unknown objects are neither adopted nor deleted.
            continue
        result["count"] += 1
        result["bytes"] += path.stat().st_size
        try:
            with closing(_readonly(path)) as connection:
                _validate(connection)
            result["verified_count"] += 1
        except BackupError:
            result["invalid_count"] += 1
    return result


def _recover(settings, state, now):
    for row in state["runs"]:
        if datetime.fromisoformat(row["started_at"]) > now:
            raise MaintenanceError("maintenance_clock_regressed")
        if row["status"] != "running":
            continue
        backup_id = row["backup_id"]
        if backup_id:
            path = bundle_directory(settings.data_dir, backup_id)
            if path.exists() or path.is_symlink():
                manifest = validate_bundle(path)
                if manifest["backup_id"] != backup_id:
                    raise MaintenanceError("maintenance_bundle_conflict")
                if not any(entry["id"] == backup_id for entry in state["bundles"]):
                    if len(state["bundles"]) >= MAX_BUNDLES:
                        raise MaintenanceError("maintenance_inventory_full")
                    state["bundles"].append(inventory_entry(path, manifest))
            partial = path.parent / f".{backup_id}.partial"
            if partial.is_symlink():
                raise MaintenanceError("maintenance_bundle_conflict")
            if partial.exists():
                directory(partial)
                _remove_partial(partial)
        row.update(
            status="interrupted",
            phase="finished",
            finished_at=now.isoformat(),
            error_code="maintenance_interrupted",
        )


def _retention(settings, state):
    verified = [entry for entry in state["bundles"] if entry["status"] == "verified"]
    if not verified:
        raise MaintenanceError("maintenance_bundle_missing")
    # Verify every retained manifest and file before making the first deletion decision.
    for entry in verified:
        actual = inventory_entry(bundle_directory(settings.data_dir, entry["id"]))
        if actual != entry:
            raise MaintenanceError("maintenance_bundle_invalid")
    policy = select_local(verified, settings.maintenance_keep_count, settings.maintenance_max_bytes)
    state["retention_over_budget"] = policy["over_budget"]
    for entry in state["bundles"]:
        if entry["id"] in policy["delete"]:
            entry["status"] = "deleting"
    write_state(settings.data_dir, state)
    for entry in list(state["bundles"]):
        if entry["status"] == "deleting":
            remove_registered_bundle(settings.data_dir, entry)
            state["bundles"].remove(entry)
            write_state(settings.data_dir, state)


def _result(settings, status, action, row=None, error_code=None):
    return {
        "status": status,
        "action": action,
        "error_code": error_code,
        "backup_id": row["backup_id"] if row else None,
        "maintenance": maintenance_status(settings),
    }


def run_maintenance(settings, *, now=None, exporter=None, retry=False):
    clock = _now(now)

    def finished():
        return clock if now is not None else datetime.now(UTC)

    day = clock.astimezone(ZONE).date().isoformat()
    state, row = None, None
    try:
        with maintenance_lock(settings.data_dir):
            state = read_state(settings.data_dir)
            _recover(settings, state, clock)
            existing = next((item for item in reversed(state["runs"]) if item["day"] == day), None)
            if existing and (not retry or existing["status"] == "completed"):
                write_state(settings.data_dir, state)
                return _result(
                    settings,
                    existing["status"],
                    "skipped_existing",
                    existing,
                    existing["error_code"],
                )
            row = {
                "day": day,
                "status": "running",
                "phase": "preparing",
                "started_at": clock.isoformat(),
                "finished_at": None,
                "error_code": None,
                "backup_id": None,
            }
            state["runs"].append(row)
            state["runs"] = state["runs"][-MAX_RUNS:]
            write_state(settings.data_dir, state)
            try:
                estimated = source_size(settings.data_dir / "sleepscape.sqlite3")
                require_capacity(
                    settings.data_dir, "backup", required_bytes=estimated * 3 + 1024**2
                )
                state["legacy"] = _legacy_inventory(settings)
                reusable = (
                    existing
                    and existing["backup_id"]
                    and any(
                        entry["id"] == existing["backup_id"] and entry["status"] == "verified"
                        for entry in state["bundles"]
                    )
                )
                if reusable:
                    row["backup_id"] = existing["backup_id"]
                    validate_bundle(
                        bundle_directory(settings.data_dir, row["backup_id"]), restore=True
                    )
                else:
                    if len(state["bundles"]) >= MAX_BUNDLES:
                        raise MaintenanceError("maintenance_inventory_full")
                    row.update(backup_id=uuid4().hex, phase="backup")
                    write_state(settings.data_dir, state)
                    state["bundles"].append(create_bundle(settings, row["backup_id"], clock))
                    write_state(settings.data_dir, state)
                row["phase"] = "export"
                write_state(settings.data_dir, state)
                mode = (
                    "none"
                    if settings.maintenance_export_mode == "disabled"
                    else settings.maintenance_export_mode
                )
                if mode == "none":
                    state["offsite"] = initial_state()["offsite"]
                else:
                    fingerprint = _export_fingerprint(settings)
                    if state["export_fingerprint"] != fingerprint:
                        state["offsite"] = initial_state()["offsite"]
                    state["export_fingerprint"] = fingerprint
                    if exporter is None:
                        from sleepscape.adapters.backup_export import export_bundle

                        exporter = export_bundle
                    try:
                        exported = exporter(
                            settings,
                            bundle_directory(settings.data_dir, row["backup_id"]),
                            now=clock,
                        )
                        if exported.get("status") != "verified":
                            raise MaintenanceError("maintenance_export_failed")
                        state["offsite"] = {
                            "status": "verified",
                            "mode": mode,
                            "last_success_at": finished().isoformat(),
                            "error_code": None,
                            "location_verified": False,
                            "retained_count": exported.get("retained_count"),
                            "retained_bytes": exported.get("retained_bytes"),
                            "over_budget": bool(exported.get("over_budget", False)),
                            "location_state": "remote_host"
                            if mode == "ssh"
                            else "location_unverified",
                            "transit_encrypted": mode == "ssh",
                            "at_rest_encryption": "not_verified",
                        }
                    except Exception:
                        state["offsite"].update(
                            status="failed",
                            mode=mode,
                            error_code="maintenance_export_failed",
                            location_verified=False,
                        )
                        raise MaintenanceError("maintenance_export_failed") from None
                row["phase"] = "retention"
                write_state(settings.data_dir, state)
                _retention(settings, state)
                row.update(status="completed", phase="finished", finished_at=finished().isoformat())
                write_state(settings.data_dir, state)
                return _result(settings, "completed", "executed", row)
            except KeyboardInterrupt:
                row.update(
                    status="interrupted",
                    phase="finished",
                    finished_at=finished().isoformat(),
                    error_code="maintenance_interrupted",
                )
                write_state(settings.data_dir, state)
                raise
            except Exception as exc:
                code = (
                    "maintenance_capacity_blocked"
                    if isinstance(exc, CapacityError)
                    else safe_code(getattr(exc, "code", str(exc)))
                )
                verified = any(item["id"] == row["backup_id"] for item in state["bundles"])
                row.update(
                    status="partial" if verified else "failed",
                    phase="finished",
                    finished_at=finished().isoformat(),
                    error_code=code,
                )
                write_state(settings.data_dir, state)
                return _result(settings, row["status"], "executed", row, code)
    except MaintenanceError as exc:
        return _result(settings, "blocked", "blocked", error_code=exc.code)
    except Exception:
        return _result(settings, "blocked", "blocked", error_code="maintenance_failed")


def check_bundle_restore(settings, backup_id):
    """Verify in a disposable copy; never replace the active database or runtime state."""
    try:
        initial = read_state(settings.data_dir)
        if not any(
            item["id"] == backup_id and item["status"] == "verified" for item in initial["bundles"]
        ):
            raise MaintenanceError("maintenance_bundle_missing")
        with maintenance_lock(settings.data_dir):
            state = read_state(settings.data_dir)
            entry = next((item for item in state["bundles"] if item["id"] == backup_id), None)
            if entry is None or entry["status"] != "verified":
                raise MaintenanceError("maintenance_bundle_missing")
            require_capacity(settings.data_dir, "backup", required_bytes=entry["size_bytes"] * 2)
            path = bundle_directory(settings.data_dir, backup_id)
            manifest = validate_bundle(path, restore=True)
            if inventory_entry(path, manifest) != entry:
                raise MaintenanceError("maintenance_bundle_invalid")
            return {
                "status": "verified",
                "backup_id": backup_id,
                "temporary_copy_removed": True,
                "runtime_files": sum(manifest["runtime_presence"].values()),
                "live_data_unchanged": True,
            }
    except CapacityError:
        raise MaintenanceError("maintenance_capacity_blocked") from None
