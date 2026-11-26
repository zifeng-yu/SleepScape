"""Synthetic derived-time migration, replay, and transaction protections."""

import copy
import json
import os
import sqlite3
from pathlib import Path

import pytest
import requests
from environment_samples import sample_profile

from sleepscape.config import Settings
from sleepscape.domain.environment import (
    EnvironmentBatch,
    canonical_json,
    content_hash,
)
from sleepscape.services.backup import BackupError, check_restore, create_backup
from sleepscape.storage import environment as facts
from sleepscape.storage import environment_index as index

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        pytest.fail("Time-index acceptance must never access a network")

    monkeypatch.setattr(requests.Session, "send", forbidden)


@pytest.fixture
def payload():
    return json.loads((FIXTURES / "environment-batch.json").read_bytes())


@pytest.fixture
def settings(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    profile = sample_profile()
    facts.register_local(settings.data_dir, "synthetic-device", "synthetic-profile", profile)
    return settings


def ingest(settings, payload):
    return facts.ingest(
        settings.data_dir, "synthetic-device", EnvironmentBatch.model_validate(payload)
    )


def rows(settings, table, columns="*"):
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        return connection.execute(f"SELECT {columns} FROM {table} ORDER BY rowid").fetchall()


def source_rows(settings):
    return {table: rows(settings, table) for table in facts.ENVIRONMENT_TABLES}


def test_ingest_does_not_implicitly_migrate_and_explicit_migration_preserves_facts(
    settings, payload
):
    ingest(settings, payload)
    before = source_rows(settings)
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='environment_time_index'"
        ).fetchone()
    index.migrate_index(settings.data_dir)
    index.rebuild_index(settings.data_dir)
    index.migrate_index(settings.data_dir)
    index.rebuild_index(settings.data_dir, force=True)
    assert source_rows(settings) == before
    assert rows(settings, "environment_time_index", "time_quality") == [("estimated",)]
    assert rows(settings, "environment_index_boots", "source_revision=indexed_revision") == [(1,)]


def test_duplicate_ingest_does_not_duplicate_index_or_change_mapping(settings, payload):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    before = rows(settings, "environment_time_index")
    for _ in range(3):
        ack = ingest(settings, payload)
        assert ack["records"][0]["status"] == "duplicate"
    assert rows(settings, "environment_time_index") == before
    assert len(rows(settings, "environment_records")) == 1
    assert rows(settings, "environment_index_boots", "source_revision=indexed_revision") == [(1,)]


def test_compatible_incremental_anchor_advances_revision_without_rewriting_history(
    settings, payload
):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    original = rows(settings, "environment_time_index")[0]
    revision = rows(settings, "environment_index_boots", "source_revision")[0][0]
    newer = copy.deepcopy(payload)
    newer["device_status"] = None
    newer["records"][0].update(seq=1, mono_start_ms=60000, mono_end_ms=120000)
    newer["time_anchors"][0].update(
        anchor_id="later-compatible", monotonic_ms=120000, utc_at="2026-01-01T00:02:00Z"
    )
    ingest(settings, newer)
    assert rows(settings, "environment_time_index")[0] == original
    versions = rows(
        settings,
        "environment_index_boots",
        "source_revision,indexed_revision,anchor_revision,indexed_anchor_revision",
    )[0]
    assert versions[0] == versions[1] == revision + 2
    assert versions[2] == versions[3] == 2
    assert rows(settings, "environment_time_index", "seq") == [(0,), (1,)]


def test_late_same_boot_anchor_rebuilds_unknown_without_rewriting_observations(settings, payload):
    index.migrate_index(settings.data_dir)
    delayed = copy.deepcopy(payload)
    delayed["time_anchors"] = []
    ingest(settings, delayed)
    before = rows(settings, "environment_records")
    assert rows(settings, "environment_time_index", "utc_start_us,time_quality") == [
        (None, "unknown")
    ]
    anchor = copy.deepcopy(payload)
    anchor["records"] = []
    anchor["device_status"] = None
    anchor["time_anchors"][0].update(
        anchor_id="late", monotonic_ms=120000, utc_at="2026-01-01T00:02:00Z"
    )
    ingest(settings, anchor)
    assert rows(settings, "environment_records") == before
    assert rows(settings, "environment_time_index", "utc_start_us,time_quality") == [
        (1767225600000000, "estimated")
    ]


def test_conflicting_anchor_invalidates_whole_boot_instead_of_overlapping(settings, payload):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    before = rows(settings, "environment_records")
    conflicting = copy.deepcopy(payload)
    conflicting["records"] = []
    conflicting["device_status"] = None
    conflicting["time_anchors"][0].update(
        anchor_id="jump", monotonic_ms=60000, utc_at="2026-01-01T00:06:00Z"
    )
    ingest(settings, conflicting)
    assert rows(settings, "environment_records") == before
    assert rows(settings, "environment_time_index", "utc_start_us,utc_end_us,time_quality") == [
        (None, None, "conflict")
    ]


def test_backfilled_boot_never_borrows_current_boot_anchor(settings, payload):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    other = copy.deepcopy(payload)
    other.update(boot_id="synthetic-older-boot", time_anchors=[], device_status=None)
    ingest(settings, other)
    assert sorted(rows(settings, "environment_time_index", "boot_id,time_quality")) == [
        ("synthetic-boot", "estimated"),
        ("synthetic-older-boot", "unknown"),
    ]


def test_new_profile_stays_bound_to_its_record_without_rewriting_old_profile(settings, payload):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    first_profile = rows(settings, "environment_profiles")
    profile = sample_profile().model_copy(update={"placement_id": "synthetic-second-placement"})
    facts.add_profile(settings.data_dir, "synthetic-device", "second-profile", profile)
    newer = copy.deepcopy(payload)
    newer.update(time_anchors=[], device_status=None)
    newer["records"][0].update(
        seq=1, measurement_profile_id="second-profile", mono_start_ms=60000, mono_end_ms=120000
    )
    ingest(settings, newer)
    assert rows(settings, "environment_profiles")[0] == first_profile[0]
    assert rows(settings, "environment_time_index", "seq,profile_id") == [
        (0, "synthetic-profile"),
        (1, "second-profile"),
    ]


def test_dirty_trigger_and_explicit_rebuild_cover_direct_imports(settings, payload):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    record = copy.deepcopy(payload["records"][0])
    record.update(seq=4, mono_start_ms=240000, mono_end_ms=300000)
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        connection.execute(
            "INSERT INTO environment_records VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "synthetic-device",
                "synthetic-boot",
                "minute",
                4,
                "synthetic-profile",
                240000,
                300000,
                canonical_json(record),
                content_hash(record),
                "2026-01-01T00:06:00Z",
            ),
        )
    assert rows(settings, "environment_index_boots", "source_revision>indexed_revision") == [(1,)]
    index.rebuild_index(settings.data_dir)
    assert rows(settings, "environment_index_boots", "source_revision=indexed_revision") == [(1,)]
    assert rows(settings, "environment_time_index", "seq") == [(0,), (4,)]


def test_refresh_failure_rolls_back_fact_anchor_audit_and_derived_index(
    settings, payload, monkeypatch
):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    before, derived = source_rows(settings), rows(settings, "environment_time_index")
    changed = copy.deepcopy(payload)
    changed["records"][0].update(seq=2, mono_start_ms=120000, mono_end_ms=180000)
    changed["time_anchors"][0].update(anchor_id="second")
    changed["device_status"]["status_seq"] = 2

    def failure(*args, **kwargs):
        raise facts.EnvironmentError("environment_index_update_failed")

    monkeypatch.setattr(index, "refresh_boot", failure)
    with pytest.raises(facts.EnvironmentError, match="environment_index_update_failed"):
        ingest(settings, changed)
    assert source_rows(settings) == before
    assert rows(settings, "environment_time_index") == derived


def test_duplicate_batch_repairs_previous_dirty_boot_before_ack(settings, payload):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        connection.execute("UPDATE environment_index_boots SET indexed_revision=indexed_revision-1")
        connection.execute("DELETE FROM environment_time_index")
    assert rows(settings, "environment_index_boots", "source_revision>indexed_revision") == [(1,)]
    before = rows(settings, "environment_records")
    ack = ingest(settings, payload)
    assert ack["records"][0]["status"] == "duplicate"
    assert rows(settings, "environment_records") == before
    assert rows(settings, "environment_index_boots", "source_revision=indexed_revision") == [(1,)]
    assert len(rows(settings, "environment_time_index")) == 1


def test_fact_conflict_cannot_leave_partially_updated_index(settings, payload):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    before, derived = source_rows(settings), rows(settings, "environment_time_index")
    conflict = copy.deepcopy(payload)
    new_record = copy.deepcopy(conflict["records"][0])
    new_record.update(seq=2, mono_start_ms=120000, mono_end_ms=180000)
    conflict["records"][0]["metrics"]["temperature_c"]["mean"] = 21.7
    conflict["records"].insert(0, new_record)
    with pytest.raises(facts.EnvironmentError, match="record_conflict"):
        ingest(settings, conflict)
    assert source_rows(settings) == before
    assert rows(settings, "environment_time_index") == derived


def test_missing_source_never_creates_database(tmp_path):
    directory = tmp_path / "not-created"
    with pytest.raises(facts.EnvironmentError):
        index.migrate_index(directory)
    with pytest.raises(facts.EnvironmentError):
        index.rebuild_index(directory)
    assert not directory.exists()


def test_backup_and_restore_include_derived_tables(settings, payload):
    index.migrate_index(settings.data_dir)
    ingest(settings, payload)
    saved = create_backup(settings)
    assert saved["counts"]["environment_time_index"] == 1
    checked = check_restore(settings, Path(saved["backup_path"]))
    assert checked["counts"]["environment_time_index"] == 1
    path = settings.data_dir / saved["backup_path"]
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE environment_time_index SET seq=9999")
    with pytest.raises(BackupError):
        check_restore(settings, path)
