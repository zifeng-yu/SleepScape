"""Explicit migration backup ordering and read-only real-record verification boundaries."""

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest
import requests
from environment_samples import sample_profile

from sleepscape.config import Settings
from sleepscape.domain.environment import EnvironmentBatch
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.services import environment_index as service
from sleepscape.services.backup import BackupError
from sleepscape.storage import environment as storage
from sleepscape.storage.environment import EnvironmentError
from sleepscape.storage.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"
PRIVATE_MARKER = "SYNTHETIC_PRIVATE_VALUE_MUST_NOT_APPEAR"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        pytest.fail("Index verification must remain offline")

    monkeypatch.setattr(requests.Session, "send", forbidden)


@pytest.fixture
def settings(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    profile = sample_profile()
    storage.register_local(settings.data_dir, "synthetic-device", "synthetic-profile", profile)
    batch = EnvironmentBatch.model_validate_json((FIXTURES / "environment-batch.json").read_bytes())
    storage.ingest(settings.data_dir, "synthetic-device", batch)
    with Repository(settings.data_dir) as repository:
        request = DataRequest("synthetic", "key", "/offline")
        run = repository.start_run({"purpose": "synthetic_index_verification"})
        private = {"marker": PRIVATE_MARKER}
        record = {
            "kind": "synthetic",
            "key": "key",
            "date": "2026-01-01",
            "timestamp": None,
            "data": private,
        }
        repository.save_snapshot(
            run,
            FetchResult(request, private, "2026-01-01T00:00:00Z", 200),
            NormalizedBatch([record]),
        )
        repository.finish_run(run, "completed", {"status": "completed"})
    return settings


def snapshot(settings):
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        return service._fingerprints(connection)


def has_index(settings):
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='environment_time_index'"
            ).fetchone()
        )


def test_first_migration_and_force_rebuild_require_verified_backup(settings, monkeypatch):
    calls = []
    for name in ("create_backup", "check_restore", "migrate_index", "rebuild_index"):
        original = getattr(service, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(service, name, wrapper)
    before = snapshot(settings)
    result = service.prepare_environment_index(settings)
    assert result["backup_verified"] is True
    assert calls == ["create_backup", "check_restore", "migrate_index", "rebuild_index"]
    assert snapshot(settings) == before
    calls.clear()
    result = service.prepare_environment_index(settings)
    assert result["backup_verified"] is False
    assert calls == ["migrate_index", "rebuild_index"]
    calls.clear()
    assert service.prepare_environment_index(settings, force=True)["backup_verified"] is True
    assert calls == ["create_backup", "check_restore", "migrate_index", "rebuild_index"]
    assert len(list((settings.data_dir / "backups").glob("*.sqlite3"))) == 2


@pytest.mark.parametrize("failure", ["create_backup", "check_restore"])
def test_failed_backup_or_restore_prevents_migration(settings, monkeypatch, failure):
    before = snapshot(settings)

    def fail(*args, **kwargs):
        raise BackupError(PRIVATE_MARKER)

    monkeypatch.setattr(service, failure, fail)
    with pytest.raises(EnvironmentError) as caught:
        service.prepare_environment_index(settings)
    assert str(caught.value) == "environment_index_prepare_failed"
    assert not has_index(settings)
    assert snapshot(settings) == before


def test_readonly_shadow_verification_replays_real_shape_without_source_migration(
    settings, monkeypatch, tmp_path, capsys
):
    directories = []
    original = service.TemporaryDirectory

    def temporary(**kwargs):
        result = original(dir=tmp_path, **kwargs)
        directories.append(Path(result.name))
        return result

    monkeypatch.setattr(service, "TemporaryDirectory", temporary)
    before = snapshot(settings)
    report = service.verify_environment_index(settings)
    assert report["status"] == "verified"
    assert report["source_index_present"] is False
    assert report["source_read_only"] is True
    assert report["mode"] == "shadow_rebuild"
    assert report["checked_at"].endswith("Z")
    assert report["fact_scope"] == "garmin_and_environment_original_tables"
    assert report["facts_unchanged"] is True
    assert report["repeated_rebuild_consistent"] is True
    assert report["indexed_record_count"] == 1
    assert report["temporary_copy_removed"] is True
    assert directories and all(not path.exists() for path in directories)
    assert snapshot(settings) == before
    assert not has_index(settings)
    assert not (settings.data_dir / "backups").exists()
    path = settings.data_dir / "reports/environment-index-verification.json"
    serialized = path.read_text()
    assert json.loads(serialized) == report
    assert PRIVATE_MARKER not in serialized + capsys.readouterr().out
    assert "synthetic-device" not in serialized
    assert "2026-01-01" not in serialized
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_shadow_matches_current_index_and_detects_corrupt_projection(settings):
    service.prepare_environment_index(settings)
    before = snapshot(settings)
    report = service.verify_environment_index(settings)
    assert report["status"] == "verified"
    assert report["source_index_current"] is True
    assert report["source_index_matches"] is True
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        connection.execute(
            "UPDATE environment_time_index SET utc_start_us=utc_start_us+60000000, "
            "utc_end_us=utc_end_us+60000000"
        )
    failed = service.verify_environment_index(settings)
    assert failed["status"] == "failed"
    assert failed["code"] == "environment_index_verification_failed"
    assert snapshot(settings) == before


def test_shadow_failure_is_redacted_and_does_not_modify_source(settings, monkeypatch):
    before = snapshot(settings)

    def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE_MARKER)

    monkeypatch.setattr(service, "rebuild_index", fail)
    failed = service.verify_environment_index(settings)
    assert failed["status"] == "failed"
    assert failed["code"] == "environment_index_verification_failed"
    assert PRIVATE_MARKER not in json.dumps(failed)
    assert snapshot(settings) == before
    assert not has_index(settings)


def test_missing_source_is_never_created(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "absent")
    with pytest.raises(EnvironmentError, match="environment_index_source_unavailable"):
        service.prepare_environment_index(settings)
    with pytest.raises(EnvironmentError, match="environment_index_report_failed"):
        service.verify_environment_index(settings)
    assert not settings.data_dir.exists()


def test_report_directory_symlink_is_rejected(settings, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (settings.data_dir / "reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(EnvironmentError, match="environment_index_report_failed"):
        service.verify_environment_index(settings)
    assert not list(outside.iterdir())


def test_prepare_does_not_promote_pending_rebuild_to_ready(settings, monkeypatch):
    service.prepare_environment_index(settings)
    monkeypatch.setattr(service, "rebuild_index", lambda *args, **kwargs: {"status": "pending"})
    assert service.prepare_environment_index(settings)["status"] == "pending"
