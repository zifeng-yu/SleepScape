"""Complete state snapshots use disposable synthetic databases and never a real account."""

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

import pytest

from sleepscape.config import Settings
from sleepscape.services import backup_bundle
from sleepscape.services.backup_bundle import create_bundle, validate_bundle
from sleepscape.storage import daily_runtime, model_runtime
from sleepscape.storage.backup_manifest import MaintenanceError
from sleepscape.storage.repository import Repository

NOW = datetime(2026, 11, 1, 1, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)
    value = Settings(_env_file=None, data_dir=tmp_path.resolve() / "data")
    with Repository(value.data_dir):
        pass
    return value


def runtime_state(settings):
    with Repository(settings.data_dir) as repository:
        run_id = repository.start_run({"synthetic": True})
        repository.finish_run(run_id, "completed", {"status": "completed"})
    with daily_runtime.collection_mutex(settings.data_dir):
        daily_runtime.write_state(
            settings.data_dir,
            {
                "schema_version": 1,
                "days": [
                    {
                        "day": "2026-11-01",
                        "status": "completed",
                        "phase": "finished",
                        "attempt": 1,
                        "started_at": NOW.isoformat(),
                        "finished_at": NOW.isoformat(),
                        "run_id": run_id,
                        "window": {"start_date": "2026-10-26", "end_date": "2026-11-01"},
                        "request_counts": {"planned": 1, "completed": 1, "failed": 0, "http": 1},
                        "error_code": None,
                        "stop_reason": None,
                        "retry_not_before": None,
                        "analysis": "skipped_deferred",
                    }
                ],
            },
        )
        daily_runtime.defer_until(settings.data_dir, (NOW + timedelta(hours=1)).isoformat())
    with model_runtime.session(settings.data_dir) as runtime:
        attempt = runtime.reserve("a" * 64, NOW)
        runtime.finish(attempt, NOW, 10, usage={"total_tokens": 1})


def large_model_runtime(settings):
    """Populate the full supported month with synthetic metadata, without any API call."""
    with model_runtime.session(settings.data_dir) as ledger:
        identity = ledger.reserve("a" * 64, NOW)
        ledger.finish(identity, NOW, 1)
    template = model_runtime.read_state(settings.data_dir)["attempts"][0]
    rows = []
    for day in range(30):
        for second in range(30):
            at = NOW - timedelta(days=29 - day) + timedelta(seconds=second)
            rows.append(
                {
                    **template,
                    "id": f"{len(rows) + 1:032x}",
                    "day": at.astimezone(model_runtime.ZONE).date().isoformat(),
                    "started_at": at.isoformat(),
                    "finished_at": at.isoformat(),
                }
            )
    model_runtime._write(settings.data_dir, {"schema_version": 1, "attempts": rows})
    raw = (settings.data_dir / model_runtime.STATE_FILE).read_bytes()
    assert 128 * 1024 < len(raw) < model_runtime.MAX_STATE_BYTES
    return raw


def test_full_month_model_ledger_is_copied_and_restored_without_changing_bytes(settings):
    original = large_model_runtime(settings)
    backup_id = uuid4().hex
    create_bundle(settings, backup_id, NOW)
    path = backup_bundle.bundle_directory(settings.data_dir, backup_id)
    validate_bundle(path, restore=True)
    assert (path / model_runtime.STATE_FILE).read_bytes() == original
    assert (settings.data_dir / model_runtime.STATE_FILE).read_bytes() == original
    assert len(model_runtime.read_state(path)["attempts"]) == 900


@pytest.mark.parametrize("name", ["daily-runtime.json", "collection-cooldown.json"])
def test_other_runtime_files_keep_their_existing_copy_size_bound(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"{}" + b" " * (128 * 1024))
    path.chmod(0o600)
    target = tmp_path / "copy"
    with pytest.raises(MaintenanceError):
        backup_bundle._copy_runtime(path, target)
    assert not target.exists()


def test_bundle_preserves_runtime_bytes_and_disposable_restore(settings):
    runtime_state(settings)
    original = {
        name: (settings.data_dir / name).read_bytes() for name in backup_bundle.RUNTIME_FILES
    }
    original_database = (settings.data_dir / "sleepscape.sqlite3").read_bytes()
    backup_id = uuid4().hex
    entry = create_bundle(settings, backup_id, NOW)
    directory = backup_bundle.bundle_directory(settings.data_dir, backup_id)
    manifest = validate_bundle(directory, restore=True)
    assert manifest["runtime_presence"] == dict.fromkeys(backup_bundle.RUNTIME_FILES, True)
    assert entry["id"] == backup_id
    for name, raw in original.items():
        assert (directory / name).read_bytes() == raw
        assert (settings.data_dir / name).read_bytes() == raw
    assert (settings.data_dir / "sleepscape.sqlite3").read_bytes() == original_database
    assert model_runtime.budget(model_runtime.read_state(directory), NOW)["used"] == 1
    assert not list(directory.parent.glob(".sleepscape-bundle-restore-*"))
    assert not any(path.name.endswith(".lock") for path in directory.iterdir())


def test_missing_runtime_files_are_explicit_and_not_created(settings):
    backup_id = uuid4().hex
    create_bundle(settings, backup_id, NOW)
    manifest = validate_bundle(backup_bundle.bundle_directory(settings.data_dir, backup_id))
    assert manifest["runtime_presence"] == dict.fromkeys(backup_bundle.RUNTIME_FILES, False)
    assert all(not (settings.data_dir / name).exists() for name in backup_bundle.RUNTIME_FILES)


@pytest.mark.parametrize("lock", [daily_runtime.collection_mutex, model_runtime.session])
def test_snapshot_refuses_active_source_without_waiting_or_partial_copy(settings, lock):
    with lock(settings.data_dir):
        with pytest.raises(MaintenanceError, match="maintenance_source_busy"):
            create_bundle(settings, uuid4().hex, NOW)
    assert not list((settings.data_dir / "backups/managed").iterdir())


@pytest.mark.parametrize("mutation", ["hash", "extra", "symlink", "fifo", "runtime_schema"])
def test_modified_or_unknown_bundle_file_is_rejected(settings, mutation):
    runtime_state(settings)
    backup_id = uuid4().hex
    create_bundle(settings, backup_id, NOW)
    path = backup_bundle.bundle_directory(settings.data_dir, backup_id)
    if mutation == "hash":
        with (path / "model-runtime.json").open("ab") as stream:
            stream.write(b" ")
    elif mutation == "extra":
        (path / "user-notes.txt").write_text("synthetic")
    elif mutation in {"symlink", "fifo"}:
        (path / "model-runtime.json").unlink()
        if mutation == "symlink":
            (path / "model-runtime.json").symlink_to(settings.data_dir / "model-runtime.json")
        else:
            os.mkfifo(path / "model-runtime.json", 0o600)
    else:
        invalid = path / "model-runtime.json"
        invalid.write_text('{"schema_version":1,"attempts":[{}]}')
        manifest = json.loads((path / "manifest.json").read_text())
        manifest["files"][invalid.name] = {
            "sha256": hashlib.sha256(invalid.read_bytes()).hexdigest(),
            "size": invalid.stat().st_size,
        }
        (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(MaintenanceError, match="maintenance_bundle_invalid"):
        validate_bundle(path, restore=True)


def test_orphan_completed_daily_reference_cannot_be_backed_up(settings):
    runtime_state(settings)
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        connection.execute("DELETE FROM runs")
    with pytest.raises(MaintenanceError, match="maintenance_bundle_invalid"):
        create_bundle(settings, uuid4().hex, NOW)


def test_snapshot_read_transaction_allows_other_sqlite_writer(settings, monkeypatch):
    entered, release = Event(), Event()
    original_copy = backup_bundle._copy

    def held(source, target):
        entered.set()
        assert release.wait(5)
        return original_copy(source, target)

    monkeypatch.setattr(backup_bundle, "_copy", held)
    results, failures = [], []

    def worker():
        try:
            results.append(create_bundle(settings, uuid4().hex, NOW))
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=worker)
    thread.start()
    assert entered.wait(5)
    try:
        with Repository(settings.data_dir) as repository:
            run = repository.start_run({"independent_writer": True})
            repository.finish_run(run, "completed", {"status": "completed"})
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not failures
    assert len(results) == 1


def test_validation_happens_after_source_locks_release(settings, monkeypatch):
    original = backup_bundle.validate_bundle
    called = []

    def check(path, **kwargs):
        with (
            daily_runtime.collection_mutex(settings.data_dir),
            model_runtime.session(settings.data_dir),
        ):
            called.append(True)
        return original(path, **kwargs)

    monkeypatch.setattr(backup_bundle, "validate_bundle", check)
    create_bundle(settings, uuid4().hex, NOW)
    assert called


def test_preexisting_partial_directory_is_not_assumed_owned(settings):
    backup_id = uuid4().hex
    destination = backup_bundle.bundle_directory(settings.data_dir, backup_id)
    destination.parent.mkdir(mode=0o700, parents=True)
    destination.parent.parent.chmod(0o700)
    partial = destination.parent / f".{backup_id}.partial"
    partial.mkdir(mode=0o700)
    evidence = partial / "model-runtime.json"
    evidence.write_text("preserve unknown existing evidence")
    with pytest.raises(MaintenanceError, match="maintenance_bundle_conflict"):
        create_bundle(settings, backup_id, NOW)
    assert evidence.read_text() == "preserve unknown existing evidence"
