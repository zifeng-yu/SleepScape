"""Backup and restore checks use disposable synthetic databases, never live data."""

import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from sleepscape import cli
from sleepscape.config import Settings
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.services import backup
from sleepscape.services.backup import BackupError, check_restore, create_backup
from sleepscape.storage import Repository

PRIVATE_VALUE = "SYNTHETIC_PRIVATE_MEASUREMENT"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper().startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)

    def fail(*args, **kwargs):
        pytest.fail("Backup tests must never make network requests")

    monkeypatch.setattr(requests.Session, "send", fail)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path / "data")


def add_record(repository, key="one"):
    request = DataRequest("synthetic", key, "/offline")
    run = repository.start_run({"purpose": "synthetic_backup"})
    payload = {"private": PRIVATE_VALUE, "key": key}
    record = {
        "kind": "synthetic",
        "key": key,
        "date": "2000-01-01",
        "timestamp": None,
        "data": payload,
    }
    repository.save_snapshot(
        run,
        FetchResult(request, payload, "2000-01-01T00:00:00Z", 200),
        NormalizedBatch([record]),
    )
    repository.finish_run(run, "completed", {"status": "completed"})


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_online_backup_sees_committed_wal_and_publishes_private_standalone_file(settings):
    with Repository(settings.data_dir) as repo:
        add_record(repo)
        before = fingerprint(repo.path)
        result = create_backup(settings)
        assert result["counts"] == repo.counts()
        assert result["record_count"] == 1
        assert fingerprint(repo.path) == before
        destination = settings.data_dir / result["backup_path"]
        assert destination.is_file()
        assert not Path(result["backup_path"]).is_absolute()
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
        assert not list(destination.parent.glob("*.tmp"))
        assert not Path(str(destination) + "-wal").exists()
        with sqlite3.connect(destination) as restored:
            assert restored.execute("SELECT COUNT(*) FROM normalized_records").fetchone()[0] == 1
    status_path = settings.data_dir / "reports/backup-status.json"
    status = json.loads(status_path.read_text())
    assert set(status) == {"last_success_at", "status", "record_count"}
    assert status["status"] == "verified"
    assert status["record_count"] == 1
    assert stat.S_IMODE(status_path.stat().st_mode) == 0o600
    assert PRIVATE_VALUE not in json.dumps(result) + status_path.read_text()


def test_each_backup_has_new_uuid_and_preserves_previous_backup(settings):
    with Repository(settings.data_dir) as repo:
        add_record(repo)
        first = create_backup(settings)
        path = settings.data_dir / first["backup_path"]
        before = fingerprint(path)
        second = create_backup(settings)
        assert second["backup_path"] != first["backup_path"]
        assert fingerprint(path) == before
        assert len(list(path.parent.glob("*.sqlite3"))) == 2


def test_name_collision_never_overwrites_existing_backup(settings, monkeypatch):
    monkeypatch.setattr(backup, "uuid4", lambda: SimpleNamespace(hex="synthetic-fixed-id"))
    with Repository(settings.data_dir) as repo:
        add_record(repo)
        result = create_backup(settings)
        path = settings.data_dir / result["backup_path"]
        original = path.read_bytes()
        with pytest.raises(BackupError, match="backup_name_collision"):
            create_backup(settings)
        assert path.read_bytes() == original


def test_restore_check_only_changes_disposable_copy_and_safe_status(
    settings, monkeypatch, tmp_path
):
    original_temporary = backup.TemporaryDirectory
    created = []

    def temporary(**kwargs):
        result = original_temporary(dir=tmp_path, **kwargs)
        created.append(Path(result.name))
        return result

    monkeypatch.setattr(backup, "TemporaryDirectory", temporary)
    with Repository(settings.data_dir) as repo:
        add_record(repo)
        saved = create_backup(settings)
        add_record(repo, "two")
        before_counts, before_bytes = repo.counts(), fingerprint(repo.path)
        copy_path = settings.data_dir / saved["backup_path"]
        before_copy = fingerprint(copy_path)
        result = check_restore(settings, Path(saved["backup_path"]))
        assert result["status"] == "verified"
        assert result["record_count"] == 1
        assert result["source_counts_match"] is True
        assert result["temporary_copy_removed"] is True
        assert repo.counts() == before_counts
        assert fingerprint(repo.path) == before_bytes
        assert fingerprint(copy_path) == before_copy
    assert created and all(not path.exists() for path in created)


def test_raw_hash_corruption_fails_without_advancing_last_success(settings):
    with Repository(settings.data_dir) as repo:
        add_record(repo)
        saved = create_backup(settings)
    status_path = settings.data_dir / "reports/backup-status.json"
    previous_status = status_path.read_bytes()
    path = settings.data_dir / saved["backup_path"]
    with sqlite3.connect(path) as corrupt:
        corrupt.execute("UPDATE raw_snapshots SET content_hash = 'incorrect'")
    with pytest.raises(BackupError, match="backup_raw_hash_failed"):
        check_restore(settings, path)
    assert status_path.read_bytes() == previous_status


def test_broken_foreign_keys_never_publish_a_backup(settings):
    with Repository(settings.data_dir) as repo:
        add_record(repo)
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as corrupt:
        corrupt.execute("UPDATE normalized_records SET snapshot_id = 999999")
    with pytest.raises(BackupError, match="backup_foreign_keys_failed"):
        create_backup(settings)
    assert not list((settings.data_dir / "backups").glob("*.sqlite3"))
    assert not list((settings.data_dir / "backups").glob(".*.tmp"))
    assert not (settings.data_dir / "reports/backup-status.json").exists()


def test_invalid_database_and_missing_source_errors_are_static(settings, tmp_path):
    with pytest.raises(BackupError, match="backup_source_unavailable"):
        create_backup(settings)
    assert not settings.data_dir.exists()
    invalid = tmp_path / PRIVATE_VALUE
    invalid.write_bytes(PRIVATE_VALUE.encode())
    with pytest.raises(BackupError) as caught:
        check_restore(settings, invalid)
    assert str(caught.value) == "backup_restore_failed"
    assert PRIVATE_VALUE not in str(caught.value)


def test_raw_decompression_limit_is_checked(settings, monkeypatch):
    with Repository(settings.data_dir) as repo:
        add_record(repo)
    monkeypatch.setattr(backup, "MAX_RAW_BYTES", 4)
    with pytest.raises(BackupError, match="backup_raw_size_or_encoding_invalid"):
        create_backup(settings)
    assert not list((settings.data_dir / "backups").glob("*.sqlite3"))


def test_empty_initialized_database_can_be_backed_up(settings):
    with Repository(settings.data_dir):
        pass
    result = create_backup(settings)
    assert result["record_count"] == 0
    assert all(value == 0 for value in result["counts"].values())
    assert check_restore(settings, Path(result["backup_path"]))["status"] == "verified"


def test_backup_cli_returns_counts_and_relative_path_only(settings, monkeypatch, capsys):
    with Repository(settings.data_dir) as repo:
        add_record(repo)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["sleepscape", "db-backup"])
    cli.main()
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "verified"
    assert PRIVATE_VALUE not in output
    assert not Path(result["backup_path"]).is_absolute()
    monkeypatch.setattr(sys, "argv", ["sleepscape", "db-restore-check", result["backup_path"]])
    cli.main()
    restored = json.loads(capsys.readouterr().out)
    assert restored["status"] == "verified"
    assert restored["temporary_copy_removed"] is True
