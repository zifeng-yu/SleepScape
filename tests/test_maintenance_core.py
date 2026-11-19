"""Maintenance failures preserve evidence and the last verified backup."""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sleepscape.config import Settings
from sleepscape.domain.retention import select_local, select_offsite
from sleepscape.services import maintenance
from sleepscape.services.backup import create_backup
from sleepscape.services.backup_bundle import bundle_directory
from sleepscape.storage.backup_manifest import (
    MaintenanceError,
    maintenance_lock,
    read_state,
    write_state,
)
from sleepscape.storage.repository import Repository
from sleepscape.storage.usage import CapacityError

NOW = datetime(2026, 11, 1, 1, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(key)
    settings = Settings(_env_file=None, data_dir=tmp_path.resolve() / "data")
    with Repository(settings.data_dir):
        pass
    return settings


def execute(settings, days=0, **kwargs):
    return maintenance.run_maintenance(settings, now=NOW + timedelta(days=days), **kwargs)


def test_daily_backup_and_restore_are_complete_and_repeat_is_idempotent(settings):
    first = execute(settings)
    assert first["status"] == "completed"
    state = read_state(settings.data_dir)
    assert len(state["bundles"]) == 1
    assert state["offsite"]["status"] == "offsite_not_configured"
    repeated = execute(settings, retry=True)
    assert repeated["action"] == "skipped_existing"
    assert repeated["backup_id"] == first["backup_id"]
    assert maintenance.check_bundle_restore(settings, first["backup_id"])["status"] == "verified"


def test_retention_keeps_latest_three_and_does_not_manage_manual_backups(settings):
    manual = create_backup(settings)
    manual_path = settings.data_dir / manual["backup_path"]
    original = manual_path.read_bytes()
    ids = [execute(settings, day)["backup_id"] for day in range(5)]
    state = read_state(settings.data_dir)
    assert {row["id"] for row in state["bundles"]} == set(ids[-3:])
    assert all(not bundle_directory(settings.data_dir, value).exists() for value in ids[:2])
    assert manual_path.read_bytes() == original
    assert state["legacy"]["count"] == state["legacy"]["verified_count"] == 1


def test_impossible_byte_budget_preserves_newest_verified_and_reports_limit(settings):
    settings.maintenance_max_bytes = 1
    execute(settings)
    result = execute(settings, 1)
    state = read_state(settings.data_dir)
    assert len(state["bundles"]) == 1
    assert state["bundles"][0]["id"] == result["backup_id"]
    assert state["retention_over_budget"] is True


def test_new_backup_failure_does_not_delete_old_verified_copy(settings, monkeypatch):
    first = execute(settings)
    old = bundle_directory(settings.data_dir, first["backup_id"])

    def fail(*args):
        raise MaintenanceError("maintenance_bundle_invalid")

    monkeypatch.setattr(maintenance, "create_bundle", fail)
    settings.maintenance_keep_count = 1
    assert execute(settings, 1)["status"] == "failed"
    assert old.is_dir()
    assert len(read_state(settings.data_dir)["bundles"]) == 1


def test_failed_export_preserves_all_copies_retry_reuses_verified_bundle(settings):
    settings.maintenance_keep_count = 1
    first = execute(settings)
    settings.maintenance_export_mode = "path"

    def unavailable(*a, **kw):
        raise RuntimeError("synthetic-secret-that-must-not-leak")

    failed = execute(settings, 1, exporter=unavailable)
    assert failed["status"] == "partial"
    assert failed["error_code"] == "maintenance_export_failed"
    assert len(read_state(settings.data_dir)["bundles"]) == 2
    assert "synthetic-secret" not in json.dumps(failed)
    completed = execute(settings, 1, retry=True, exporter=lambda *a, **kw: {"status": "verified"})
    assert completed["status"] == "completed"
    assert completed["backup_id"] == failed["backup_id"]
    assert not bundle_directory(settings.data_dir, first["backup_id"]).exists()


def test_capacity_block_before_copy_preserves_only_copy(settings, monkeypatch):
    first = execute(settings)

    def blocked(*a, **kw):
        raise CapacityError("storage_capacity_restricted")

    monkeypatch.setattr(maintenance, "require_capacity", blocked)
    second = execute(settings, 1)
    assert second["error_code"] == "maintenance_capacity_blocked"
    assert bundle_directory(settings.data_dir, first["backup_id"]).exists()
    assert len(read_state(settings.data_dir)["bundles"]) == 1


def test_interrupted_registered_deletion_replays_without_losing_latest(settings, monkeypatch):
    settings.maintenance_keep_count = 1
    first = execute(settings)
    original = maintenance.remove_registered_bundle

    def interrupted(data_dir, entry):
        (bundle_directory(data_dir, entry["id"]) / "sleepscape.sqlite3").unlink()
        raise KeyboardInterrupt

    monkeypatch.setattr(maintenance, "remove_registered_bundle", interrupted)
    with pytest.raises(KeyboardInterrupt):
        execute(settings, 1)
    state = read_state(settings.data_dir)
    newest = state["runs"][-1]["backup_id"]
    assert bundle_directory(settings.data_dir, newest).is_dir()
    assert state["bundles"][0]["status"] == "deleting"
    monkeypatch.setattr(maintenance, "remove_registered_bundle", original)
    assert execute(settings, 1, retry=True)["status"] == "completed"
    assert not bundle_directory(settings.data_dir, first["backup_id"]).exists()
    assert [row["id"] for row in read_state(settings.data_dir)["bundles"]] == [newest]


def test_unknown_file_prevents_retention_and_is_not_removed(settings):
    settings.maintenance_keep_count = 1
    first = execute(settings)
    unknown = bundle_directory(settings.data_dir, first["backup_id"]) / "user-notes.txt"
    unknown.write_text("must preserve")
    assert execute(settings, 1)["status"] == "partial"
    assert unknown.read_text() == "must preserve"
    assert len(read_state(settings.data_dir)["bundles"]) == 2


@pytest.mark.parametrize("kind", ["invalid", "permissions", "symlink", "fifo"])
def test_bad_manifest_blocks_work_and_readonly_status_stays_safe(settings, kind):
    target = settings.data_dir / "maintenance-state.json"
    if kind == "symlink":
        target.symlink_to(settings.data_dir / "sleepscape.sqlite3")
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.write_text("synthetic-private-secret")
        target.chmod(0o644 if kind == "permissions" else 0o600)
    result = execute(settings)
    assert result["status"] == "blocked"
    assert result["maintenance"]["state"] == "unavailable"
    assert result["maintenance"]["backups"]["verified_count"] is None
    assert "synthetic-private" not in json.dumps(result)
    assert not (settings.data_dir / "backups").exists()


def test_readonly_plan_does_not_create_data_directory(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path.resolve() / "absent")
    result = maintenance.plan_maintenance(settings, now=NOW)
    assert result["runtime_state"] == "not_run"
    assert result["schedule"]["time"] == "04:00"
    assert not settings.data_dir.exists()


def test_maintenance_mutex_blocks_parallel_run(settings):
    with maintenance_lock(settings.data_dir):
        result = execute(settings)
    assert result["error_code"] == "maintenance_already_running"
    assert not (settings.data_dir / "backups").exists()


def test_completed_snapshot_published_before_state_commit_recovers(settings):
    first = execute(settings)
    state = read_state(settings.data_dir)
    state["bundles"] = []
    state["runs"][-1].update(status="running", phase="backup", finished_at=None)
    write_state(settings.data_dir, state)
    assert execute(settings)["status"] == "interrupted"
    assert len(read_state(settings.data_dir)["bundles"]) == 1
    result = execute(settings, retry=True)
    assert result["backup_id"] == first["backup_id"]
    assert len(read_state(settings.data_dir)["bundles"]) == 1


def test_export_config_change_does_not_reuse_old_verification(settings):
    settings.maintenance_export_mode = "path"
    settings.maintenance_export_path = settings.data_dir.parent / "first"
    execute(settings, exporter=lambda *a, **kw: {"status": "verified"})
    assert maintenance.maintenance_status(settings)["offsite"]["status"] == "verified"
    settings.maintenance_export_path = settings.data_dir.parent / "second"
    assert maintenance.maintenance_status(settings)["offsite"]["status"] == "not_checked"


def test_retention_pure_policies_protect_latest_and_deduplicate_daily_weekly():
    entries = [
        {"id": uuid4().hex, "created_at": (NOW + timedelta(days=day)).isoformat(), "size_bytes": 10}
        for day in range(30)
    ]
    local = select_local(entries, 3, 20)
    assert local["keep"] == [row["id"] for row in entries[-1:-3:-1]]
    offsite = select_offsite(entries, 7, 4, 1000)
    assert entries[-1]["id"] in offsite["keep"]
    assert len(offsite["keep"]) <= 11
    assert len(offsite["keep"]) == len(set(offsite["keep"]))
    assert len(offsite["keep"] + offsite["delete"]) == 30
