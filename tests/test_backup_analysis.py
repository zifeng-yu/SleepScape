"""Readiness checks are part of verified SQLite and managed-bundle recovery."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import requests

from sleepscape.config import Settings
from sleepscape.domain.analysis_readiness import evaluate_readiness
from sleepscape.services import backup_bundle
from sleepscape.services.backup import BackupError, check_restore, create_backup
from sleepscape.storage import analysis
from sleepscape.storage.backup_manifest import MaintenanceError, safe_code, write_json
from sleepscape.storage.repository import Repository


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(key)

    def forbidden(*args, **kwargs):
        pytest.fail("Readiness backup tests must not access a network")

    monkeypatch.setattr(requests.Session, "send", forbidden)
    value = Settings(_env_file=None, data_dir=tmp_path / "data")
    with Repository(value.data_dir):
        pass
    analysis.migrate_analysis(value.data_dir)
    result = {
        "policy_version": "dual-source-strict-1",
        "night_date": "2001-01-02",
        "checked_at": "2001-01-02T10:00:00Z",
        "data_state": "ready",
        "core_ready": True,
        "window": {
            "start": "2001-01-02T00:00:00Z",
            "end": "2001-01-02T08:00:00Z",
            "duration_seconds": 28800,
        },
        "missing": [],
        "optional_missing": [],
        "garmin_summary": {},
        "environment_summary": {},
        "limitations": [],
        "source_fingerprint": "a" * 64,
    }
    analysis.save_results(value.data_dir, [result], run_id=str(uuid4()))
    analysis.save_results(
        value.data_dir, [{**result, "checked_at": "2001-01-02T11:00:00Z"}], run_id=str(uuid4())
    )
    return value


def mutate(path, corruption):
    with closing(sqlite3.connect(path)) as connection, connection:
        if corruption == "schema":
            connection.execute("DROP TRIGGER analysis_readiness_checks_no_update")
        elif corruption == "hash":
            connection.execute("DROP TRIGGER analysis_readiness_checks_no_update")
            connection.execute(
                "UPDATE analysis_readiness_checks SET payload_hash=? WHERE check_id=1", ("b" * 64,)
            )
            connection.execute(analysis._DDL["analysis_readiness_checks_no_update"])
        elif corruption == "missing_projection":
            connection.execute("DELETE FROM analysis_readiness")
        elif corruption == "old_projection":
            connection.execute("UPDATE analysis_readiness SET check_id=1")
        else:
            connection.execute("UPDATE analysis_readiness SET night_date='2001-01-03'")


def test_sqlite_backup_restores_audit_and_current_references(settings):
    before = analysis.read_latest(settings.data_dir, "2001-01-02")
    saved = create_backup(settings)
    assert {key: saved["counts"][key] for key in analysis.ANALYSIS_TABLES} == {
        "analysis_schema": 1,
        "analysis_readiness_checks": 2,
        "analysis_readiness": 1,
    }
    checked = check_restore(settings, Path(saved["backup_path"]))
    assert checked["counts"] == saved["counts"]
    assert checked["temporary_copy_removed"]
    with closing(sqlite3.connect(settings.data_dir / saved["backup_path"])) as connection:
        analysis.validate_analysis(connection)
        restored = analysis.read_latest(settings.data_dir, "2001-01-02", connection=connection)
    assert restored == before == analysis.read_latest(settings.data_dir, "2001-01-02")
    assert "source_fingerprint" not in str(saved) + str(checked)


@pytest.mark.parametrize(
    "corruption", ["schema", "hash", "missing_projection", "old_projection", "wrong_night"]
)
def test_bad_analysis_recovery_is_rejected_even_when_sqlite_integrity_passes(settings, corruption):
    saved = create_backup(settings)
    path = settings.data_dir / saved["backup_path"]
    mutate(path, corruption)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    code = "backup_analysis_schema_invalid" if corruption == "schema" else "backup_analysis_invalid"
    if corruption == "wrong_night":
        code = "backup_foreign_keys_failed"
    with pytest.raises(BackupError, match=f"^{code}$"):
        check_restore(settings, path)
    assert analysis.read_latest(settings.data_dir, "2001-01-02")["check_id"] == 2


def test_managed_bundle_contains_and_restores_analysis_without_new_bundle_format(settings):
    backup_id = uuid4().hex
    backup_bundle.create_bundle(settings, backup_id, datetime(2001, 1, 2, 12, tzinfo=UTC))
    path = backup_bundle.bundle_directory(settings.data_dir, backup_id)
    manifest = backup_bundle.validate_bundle(path, restore=True)
    assert manifest["schema_version"] == 1
    assert manifest["counts"]["analysis_readiness_checks"] == 2
    assert manifest["counts"]["analysis_readiness"] == 1
    assert analysis.read_latest(path, "2001-01-02") == analysis.read_latest(
        settings.data_dir, "2001-01-02"
    )
    assert not list(path.parent.glob(".sleepscape-bundle-restore-*"))


def test_managed_bundle_validates_analysis_beyond_file_hash_and_row_counts(settings):
    backup_id = uuid4().hex
    backup_bundle.create_bundle(settings, backup_id, datetime(2001, 1, 2, 12, tzinfo=UTC))
    path = backup_bundle.bundle_directory(settings.data_dir, backup_id)
    manifest = backup_bundle.validate_bundle(path)
    mutate(path / backup_bundle.DATABASE, "old_projection")
    manifest["files"][backup_bundle.DATABASE] = backup_bundle.file_digest(
        path / backup_bundle.DATABASE
    )
    write_json(path / backup_bundle.MANIFEST, manifest)
    with pytest.raises(MaintenanceError, match="maintenance_bundle_invalid"):
        backup_bundle.validate_bundle(path, restore=True)


def test_analysis_backup_error_codes_are_retained_in_safe_maintenance_state():
    assert safe_code("backup_analysis_schema_invalid") == "backup_analysis_schema_invalid"
    assert safe_code("backup_analysis_invalid") == "backup_analysis_invalid"


def add_current_check(settings):
    value = evaluate_readiness(
        date(2001, 1, 2), None, None, now=datetime(2001, 1, 2, 12, tzinfo=UTC)
    )
    value["source_fingerprint"] = "b" * 64
    analysis.save_results(settings.data_dir, [value], run_id=str(uuid4()))
    return value


def all_checks(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM analysis_readiness_checks ORDER BY check_id"
        ).fetchall()


@pytest.mark.parametrize("managed", [False, True])
def test_mixed_policy_backup_restore_preserves_old_audit_without_rewriting(settings, managed):
    database = settings.data_dir / "sleepscape.sqlite3"
    old_checks = all_checks(database)
    add_current_check(settings)
    expected = all_checks(database)
    assert expected[:2] == old_checks
    if managed:
        backup_id = uuid4().hex
        backup_bundle.create_bundle(settings, backup_id, datetime(2001, 1, 2, 13, tzinfo=UTC))
        path = backup_bundle.bundle_directory(settings.data_dir, backup_id)
        checked = backup_bundle.validate_bundle(path, restore=True)
        restored_database = path / backup_bundle.DATABASE
        assert checked["schema_version"] == 1
    else:
        saved = create_backup(settings)
        restored_database = settings.data_dir / saved["backup_path"]
        checked = check_restore(settings, Path(saved["backup_path"]))
    assert checked["counts"]["analysis_readiness_checks"] == 3
    assert checked["counts"]["analysis_readiness"] == 1
    assert all_checks(restored_database) == expected == all_checks(database)
    with closing(sqlite3.connect(restored_database)) as connection:
        analysis.validate_analysis(connection)
        read = analysis.read_latest(settings.data_dir, "2001-01-02", connection=connection)
        assert read["policy_version"] == "dual-source-coverage-1"
        assert read["quality_state"] == "insufficient"
        assert connection.execute("SELECT version FROM analysis_schema").fetchone()[0] == 1
        payloads = connection.execute(
            "SELECT payload_json FROM analysis_readiness_checks ORDER BY check_id"
        ).fetchall()
        assert "quality_state" not in json.loads(payloads[0][0])
        assert "quality_state" not in json.loads(payloads[1][0])


def test_restore_rejects_new_policy_tampering_even_with_recomputed_payload_hash(settings):
    add_current_check(settings)
    saved = create_backup(settings)
    path = settings.data_dir / saved["backup_path"]
    with closing(sqlite3.connect(path)) as connection, connection:
        original = connection.execute(
            "SELECT payload_json FROM analysis_readiness_checks WHERE check_id=3"
        ).fetchone()[0]
        payload = json.loads(original)
        payload["policy"]["min_channel_coverage"] = 0.5
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        connection.execute("DROP TRIGGER analysis_readiness_checks_no_update")
        connection.execute(
            "UPDATE analysis_readiness_checks SET payload_json=?,payload_hash=? WHERE check_id=3",
            (encoded, hashlib.sha256(encoded.encode()).hexdigest()),
        )
        connection.execute(analysis._DDL["analysis_readiness_checks_no_update"])
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(BackupError, match="^backup_analysis_invalid$"):
        check_restore(settings, path)
    assert (
        analysis.read_latest(settings.data_dir, "2001-01-02")["policy"]["min_channel_coverage"]
        == 0.99
    )


def test_conflicting_sources_diagnostic_can_be_saved_and_restored(settings):
    start, end = "2001-01-02T00:00:00Z", "2001-01-02T00:01:00Z"
    night = {"date": "2001-01-02", "sleep": {"start_at": start, "end_at": end}}
    environment = {
        "state": "available",
        "truncated": False,
        "sources": [],
        "global_time_state": {},
        "records": [
            {
                "record_ref": f"record-{i}",
                "source_ref": f"source-{i}",
                "boot_ref": f"boot-{i}",
                "record_type": "minute",
                "utc_start": start,
                "utc_end": end,
                "duration_ms": 60000,
                "time_quality": "estimated",
                "time_flags": [],
                "anchor_ids": [f"anchor-{i}"],
                "quality": [],
                "metrics": {},
            }
            for i in range(3)
        ],
    }
    result = evaluate_readiness(
        date(2001, 1, 2), night, environment, now=datetime(2001, 1, 2, 12, tzinfo=UTC)
    )
    result["source_fingerprint"] = "b" * 64
    summary = result["environment_summary"]
    assert summary["observed_windows"] == 3
    assert summary["overlap_seconds"] == 60
    assert set(summary["channels"].values()) == {None}
    assert {item["code"] for item in result["missing"]} >= {
        "configuration_changed",
        "time_conflict",
    }
    analysis.save_results(settings.data_dir, [result], run_id=str(uuid4()))
    saved = create_backup(settings)
    checked = check_restore(settings, Path(saved["backup_path"]))
    assert checked["counts"]["analysis_readiness_checks"] == 3
    read = analysis.read_latest(settings.data_dir, "2001-01-02")
    assert read["environment_summary"] == summary
