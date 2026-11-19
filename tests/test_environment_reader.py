"""Synthetic acceptance for bounded, coherent, fail-closed environment reads."""

import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.storage import environment
from sleepscape.storage.environment_index import migrate_index, rebuild_index
from sleepscape.storage.environment_reader import WINDOW_SQL, EnvironmentReader, instant_us
from sleepscape.storage.reader import StorageReadError
from sleepscape.storage.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"
START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def seeded(tmp_path):
    data_dir = tmp_path / "synthetic"
    profile = MeasurementProfile.model_validate_json(
        (FIXTURES / "environment-profile.json").read_bytes()
    )
    payload = json.loads((FIXTURES / "environment-batch.json").read_bytes())
    environment.register_local(data_dir, payload["device_id"], "synthetic-profile", profile)
    environment.ingest(data_dir, payload["device_id"], EnvironmentBatch.model_validate(payload))
    migrate_index(data_dir)
    rebuild_index(data_dir)
    return data_dir, payload, profile


def test_missing_database_and_missing_environment_are_read_only(tmp_path):
    absent = tmp_path / "absent"
    with EnvironmentReader(absent) as reader:
        assert reader.timeline(START, START + timedelta(hours=8))["state"] == "not_initialized"
        assert reader.status()["devices"] == []
    assert not absent.exists()
    with Repository(tmp_path / "base"):
        pass
    with EnvironmentReader(tmp_path / "base") as reader:
        assert reader.state == "not_initialized"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.connection.execute("CREATE TABLE forbidden(value)")


def test_unmigrated_environment_is_reported_without_implicit_migration(tmp_path):
    environment.initialize(tmp_path)
    with EnvironmentReader(tmp_path) as reader:
        assert reader.state == "index_not_initialized"
        assert reader.timeline(START, START + timedelta(minutes=1))["records"] == []
    with sqlite3.connect(tmp_path / "sleepscape.sqlite3") as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='environment_time_index'"
        ).fetchone()


def test_unmigrated_status_does_not_scan_received_history(tmp_path):
    profile = MeasurementProfile.model_validate_json(
        (FIXTURES / "environment-profile.json").read_bytes()
    )
    environment.register_local(tmp_path, "synthetic-device", "synthetic-profile", profile)
    with EnvironmentReader(tmp_path) as reader:
        queries = []
        reader.connection.set_trace_callback(queries.append)
        result = reader.status()
        assert result["devices"][0]["capture_state"] == "not_indexed"
        assert result["devices"][0]["receive_state"] == "not_indexed"
        assert not any("FROM environment_ingest_batches" in query for query in queries)
        assert not any("FROM environment_device_status" in query for query in queries)


def test_latest_capture_work_is_bounded_when_many_clean_boots_exist(seeded):
    data_dir, payload, _ = seeded
    with sqlite3.connect(data_dir / "sleepscape.sqlite3") as connection:
        connection.executemany(
            "INSERT INTO environment_index_boots(device_id,boot_id) VALUES (?,?)",
            [(payload["device_id"], f"synthetic-extra-{number}") for number in range(512)],
        )
    with EnvironmentReader(data_dir) as reader:
        queries = []
        reader.connection.set_trace_callback(queries.append)
        result = reader.status()
        assert result["devices"][0]["capture_state"] == "boot_limit_exceeded"
        assert result["devices"][0]["last_mapped_capture"] is None
        assert not any("INDEXED BY environment_time_index_boot_utc" in query for query in queries)


@pytest.mark.parametrize("table", ["environment_profiles", "environment_index_boots"])
def test_partial_or_corrupt_schema_is_not_empty_data(seeded, table):
    data_dir, _, _ = seeded
    with sqlite3.connect(data_dir / "sleepscape.sqlite3") as connection:
        connection.execute(f"DROP TABLE {table}")
    with pytest.raises(StorageReadError):
        with EnvironmentReader(data_dir):
            pass


def test_window_is_half_open_and_uses_both_index_bounds(seeded):
    data_dir, _, _ = seeded
    with EnvironmentReader(data_dir) as reader:
        row = reader.timeline(START + timedelta(seconds=59), START + timedelta(minutes=1))
        assert len(row["records"]) == 1
        assert row["records"][0]["mono_start_ms"] == 0
        assert (
            reader.timeline(START + timedelta(minutes=1), START + timedelta(minutes=2))["records"]
            == []
        )
        plan = reader.connection.execute(
            "EXPLAIN QUERY PLAN " + WINDOW_SQL,
            (instant_us(START) - 60_000_000, instant_us(START) + 60_000_000, instant_us(START), 2),
        ).fetchall()
        descriptions = " ".join(row[3] for row in plan)
        assert "environment_time_index_utc" in descriptions
        assert "utc_start_us>? AND utc_start_us<?" in descriptions


def test_device_filter_uses_local_identity_only(seeded):
    data_dir, payload, _ = seeded
    with EnvironmentReader(data_dir) as reader:
        assert (
            len(
                reader.timeline(
                    START, START + timedelta(minutes=1), device_id=payload["device_id"]
                )["records"]
            )
            == 1
        )
        assert not reader.timeline(START, START + timedelta(minutes=1), device_id="not-selected")[
            "records"
        ]


def test_dirty_boot_cannot_return_previous_utc_and_read_does_not_rebuild(seeded):
    data_dir, _, _ = seeded
    # Ordinary ingestion updates the index atomically. Simulate a stale persisted
    # revision left by a deferred explicit migration/maintenance operation.
    with sqlite3.connect(data_dir / "sleepscape.sqlite3") as connection:
        connection.execute("UPDATE environment_index_boots SET source_revision=source_revision+1")
    with EnvironmentReader(data_dir) as reader:
        result = reader.timeline(START, START + timedelta(minutes=1))
        assert result["records"] == []
        assert result["global_time_state"]["stale_boot_count"] == 1
        assert result["global_time_state"]["source_record_count"] == 1
        assert result["global_time_state"]["current_index_record_count"] == 0
        assert result["global_time_state"]["stale_source_record_count"] == 1
        assert result["global_time_state"]["scope"] == "all_history_not_window"
    rebuild_index(data_dir)
    with EnvironmentReader(data_dir) as reader:
        assert len(reader.timeline(START, START + timedelta(minutes=1))["records"]) == 1


def test_new_boot_unknown_time_only_counts_in_global_summary(seeded):
    data_dir, payload, _ = seeded
    unknown = copy.deepcopy(payload)
    unknown["boot_id"] = "unknown-boot"
    unknown["time_anchors"] = []
    unknown["device_status"] = None
    environment.ingest(data_dir, payload["device_id"], EnvironmentBatch.model_validate(unknown))
    rebuild_index(data_dir)
    with EnvironmentReader(data_dir) as reader:
        result = reader.timeline(START + timedelta(days=2), START + timedelta(days=2, minutes=1))
        assert result["records"] == []
        assert result["global_time_state"]["unknown_record_count"] == 1
        assert result["global_time_state"]["scope"] == "all_history_not_window"


def test_one_snapshot_survives_concurrent_ingestion(seeded):
    data_dir, payload, _ = seeded
    changed = copy.deepcopy(payload)
    changed["boot_id"] = "new-boot"
    with EnvironmentReader(data_dir) as reader:
        before = reader.timeline(START, START + timedelta(minutes=1))
        environment.ingest(data_dir, payload["device_id"], EnvironmentBatch.model_validate(changed))
        rebuild_index(data_dir)
        after = reader.timeline(START, START + timedelta(minutes=1))
        assert after == before
    with EnvironmentReader(data_dir) as reader:
        assert len(reader.timeline(START, START + timedelta(minutes=1))["records"]) == 2


def test_snapshot_with_new_compatible_anchor_keeps_version_consistent(seeded):
    data_dir, payload, _ = seeded
    late = copy.deepcopy(payload)
    late["records"] = []
    late["device_status"] = None
    late["time_anchors"][0].update(
        anchor_id="later", monotonic_ms=120000, utc_at="2026-01-01T00:02:00Z"
    )
    with EnvironmentReader(data_dir) as reader:
        before = reader.timeline(START, START + timedelta(minutes=1))["records"][0]
        environment.ingest(data_dir, payload["device_id"], EnvironmentBatch.model_validate(late))
        assert reader.timeline(START, START + timedelta(minutes=1))["records"][0] == before
    with EnvironmentReader(data_dir) as reader:
        after = reader.timeline(START, START + timedelta(minutes=1))["records"][0]
        assert after["mapping_version"] == before["mapping_version"]
        assert after["time_index_revision"] > before["time_index_revision"]


@pytest.mark.parametrize("damage", ["algorithm", "trigger", "mapping_version"])
def test_invalid_index_cannot_be_displayed_as_available(seeded, damage):
    data_dir, _, _ = seeded
    with sqlite3.connect(data_dir / "sleepscape.sqlite3") as connection:
        if damage == "algorithm":
            connection.execute("UPDATE environment_index_schema SET algorithm='unknown-algorithm'")
        elif damage == "trigger":
            connection.execute("DROP TRIGGER environment_index_records_insert")
        else:
            connection.execute(
                "UPDATE environment_time_index SET mapping_version=mapping_version+1"
            )
    with pytest.raises(StorageReadError):
        with EnvironmentReader(data_dir) as reader:
            reader.timeline(START, START + timedelta(minutes=1))


def test_limit_plus_one_marks_truncation_and_duplicate_upload_does_not_add(seeded):
    data_dir, payload, _ = seeded
    additional = copy.deepcopy(payload)
    additional["records"][0]["seq"] = 1
    additional["records"][0]["mono_start_ms"] = 60000
    additional["records"][0]["mono_end_ms"] = 120000
    for _ in range(2):
        environment.ingest(
            data_dir, payload["device_id"], EnvironmentBatch.model_validate(additional)
        )
    rebuild_index(data_dir)
    with EnvironmentReader(data_dir) as reader:
        result = reader.timeline(START, START + timedelta(minutes=2), limit=1)
        assert len(result["records"]) == 1 and result["truncated"]
        complete = reader.timeline(START, START + timedelta(minutes=2), limit=2)
        assert len(complete["records"]) == 2 and not complete["truncated"]


def test_invalid_payload_is_an_error_not_empty_or_raw_text(seeded):
    data_dir, _, _ = seeded
    with sqlite3.connect(data_dir / "sleepscape.sqlite3") as connection:
        connection.execute("UPDATE environment_records SET payload_json='synthetic-private-marker'")
    with EnvironmentReader(data_dir) as reader, pytest.raises(StorageReadError) as caught:
        reader.timeline(START, START + timedelta(minutes=1))
    assert str(caught.value) == "environment_data_invalid"


@pytest.mark.parametrize("limit", [0, 10001, True, 2.5])
def test_limit_is_bounded_even_without_database(tmp_path, limit):
    with EnvironmentReader(tmp_path) as reader, pytest.raises(ValueError):
        reader.timeline(START, START + timedelta(minutes=1), limit=limit)


@pytest.mark.parametrize("hours", [0, -1, 49])
def test_window_is_bounded_even_without_database(tmp_path, hours):
    with EnvironmentReader(tmp_path) as reader, pytest.raises(ValueError):
        reader.timeline(START, START + timedelta(hours=hours))


def test_naive_window_rejected(tmp_path):
    with EnvironmentReader(tmp_path) as reader, pytest.raises(ValueError):
        reader.timeline(START.replace(tzinfo=None), START + timedelta(minutes=1))
