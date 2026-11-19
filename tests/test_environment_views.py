"""Synthetic white-list, gap, configuration, and device-status environment views."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleepscape.config import Settings
from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.services.environment_views import EnvironmentViewService
from sleepscape.storage import environment
from sleepscape.storage.environment_index import migrate_index, rebuild_index

FIXTURES = Path(__file__).parent / "fixtures"
START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def view(tmp_path, monkeypatch):
    profile = MeasurementProfile.model_validate_json(
        (FIXTURES / "environment-profile.json").read_bytes()
    )
    payload = json.loads((FIXTURES / "environment-batch.json").read_bytes())
    settings = Settings(_env_file=None, data_dir=tmp_path / "synthetic", demo=True)
    monkeypatch.setattr(environment, "_now", lambda: "2026-01-01T00:01:01Z")
    environment.register_local(
        settings.data_dir, payload["device_id"], "synthetic-profile", profile
    )
    environment.ingest(
        settings.data_dir, payload["device_id"], EnvironmentBatch.model_validate(payload)
    )
    migrate_index(settings.data_dir)
    rebuild_index(settings.data_dir)
    return EnvironmentViewService(settings), payload, profile


def add(service, payload):
    environment.ingest(
        service.settings.data_dir, payload["device_id"], EnvironmentBatch.model_validate(payload)
    )
    rebuild_index(service.settings.data_dir)


def test_unavailable_index_does_not_present_a_complete_or_missing_night(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "absent", demo=True)
    result = EnvironmentViewService(settings).timeline(START, START + timedelta(hours=8))
    assert result["state"] == "not_initialized"
    assert result["coverage"] is None
    assert result["global_time_state"]["unknown_record_count"] is None
    assert not result["coverage_is_channel_completeness"]
    assert not settings.data_dir.exists()


def test_public_view_preserves_granularity_and_never_exposes_identity_or_raw_payload(view):
    service, payload, profile = view
    result = service.timeline(START + timedelta(seconds=30), START + timedelta(minutes=1))
    assert result["demo"] is True
    row = result["records"][0]
    assert row["duration_ms"] == 60000
    assert row["utc_start"] == "2026-01-01T00:00:00Z"
    assert row["time_quality"] == "estimated"
    assert row["time_uncertainty_ms"] is None
    assert row["anchor_ids"][0].startswith("anchor-")
    assert result["coverage"]["covered_duration_ms"] == 30000
    assert row["metrics"]["temperature_c"]["expected_count"] == 6
    assert row["metrics"]["sound_relative"]["unit"] == "dBFS"
    encoded = json.dumps(result)
    for private in (
        payload["device_id"],
        payload["boot_id"],
        "synthetic-profile",
        profile.placement_id,
        "payload_json",
        "token_hash",
        "content_hash",
        "measurement_profile_id",
        str(service.settings.data_dir),
    ):
        assert private not in encoded
    again = service.timeline(START, START + timedelta(minutes=1))
    assert again["records"][0]["source_ref"] == row["source_ref"]


def test_gap_stays_gap_and_single_invalid_channel_stays_null(view):
    service, payload, _ = view
    late = copy.deepcopy(payload)
    late["records"][0].update(seq=2, mono_start_ms=120000, mono_end_ms=180000)
    missing = late["records"][0]["metrics"]["relative_humidity_pct"]
    missing.update(
        state="missing",
        valid_count=0,
        observed_count=0,
        mean=None,
        min=None,
        max=None,
        quality=["missing"],
    )
    add(service, late)
    result = service.timeline(START, START + timedelta(minutes=3))
    assert result["coverage"]["gaps"] == [
        {"utc_start": "2026-01-01T00:01:00Z", "utc_end": "2026-01-01T00:02:00Z"}
    ]
    metrics = result["records"][1]["metrics"]
    assert metrics["relative_humidity_pct"]["mean"] is None
    assert metrics["temperature_c"]["mean"] is not None
    assert not result["coverage_is_channel_completeness"]


def test_restarts_and_configuration_changes_keep_distinct_segments_and_overlap(view):
    service, payload, profile = view
    replacement = profile.model_copy(update={"placement_id": "synthetic-moved"})
    environment.add_profile(service.settings.data_dir, payload["device_id"], "moved", replacement)
    second = copy.deepcopy(payload)
    second["boot_id"] = "restarted"
    second["records"][0]["measurement_profile_id"] = "moved"
    second["time_anchors"][0]["utc_at"] = "2026-01-01T00:00:30Z"
    add(service, second)
    result = service.timeline(START, START + timedelta(minutes=2))
    assert len(result["sources"]) == 2
    assert len(result["segments"]) == 2
    assert len({segment["boot_ref"] for segment in result["segments"]}) == 2
    assert len({source["placement_ref"] for source in result["sources"]}) == 2
    assert result["coverage"]["overlap_duration_ms"] == 30000
    assert result["coverage"]["covered_duration_ms"] == 90000
    assert result["source_boundaries_preserved"]
    assert result["coverage_scope"] == "mapped_minute_windows_only"


def test_truncation_never_claims_dropped_rows_are_gaps(view):
    service, payload, _ = view
    second = copy.deepcopy(payload)
    second["records"][0].update(seq=1, mono_start_ms=60000, mono_end_ms=120000)
    add(service, second)
    result = service.timeline(START, START + timedelta(minutes=2), limit=1)
    assert result["truncated"]
    assert result["coverage"] is None
    assert result["segments"][0]["coverage"] is None


def test_unknown_time_is_global_count_not_an_event_on_the_requested_night(view):
    service, payload, _ = view
    unknown = copy.deepcopy(payload)
    unknown["boot_id"] = "unanchored"
    unknown["time_anchors"] = []
    add(service, unknown)
    result = service.timeline(START, START + timedelta(minutes=1))
    assert len(result["records"]) == 1
    assert result["global_time_state"]["unknown_record_count"] == 1
    assert result["global_time_state"]["scope"] == "all_history_not_window"


def test_event_envelope_does_not_imply_continuous_sound(view):
    service, payload, _ = view
    event = copy.deepcopy(payload)
    event["records"] = [
        {
            "record_type": "event",
            "seq": 0,
            "measurement_profile_id": "synthetic-profile",
            "mono_start_ms": 0,
            "mono_end_ms": 60000,
            "quality": ["merged"],
            "event_kind": "relative_sound",
            "active_duration_ms": 2000,
            "max_1s_rms_dbfs": -30.0,
            "baseline_dbfs": None,
            "threshold_profile_id": "relative-sound-60s-10db-6db",
            "merged": True,
            "incomplete": False,
        }
    ]
    add(service, event)
    result = service.timeline(START, START + timedelta(minutes=1))
    item = next(row for row in result["records"] if row["record_type"] == "event")
    assert item["event"]["active_duration_ms"] == 2000
    assert not item["event"]["envelope_is_continuous_sound"]
    assert result["coverage"]["overlap_duration_ms"] == 0


def test_received_freshness_capture_and_queue_snapshot_are_separate(view):
    service, _, _ = view
    result = service.status(now=START + timedelta(minutes=2))
    row = result["devices"][0]
    assert row["receive_state"] == "recently_received"
    assert row["currently_collecting"] is None
    assert row["last_mapped_capture"]["utc_end"] == "2026-01-01T00:01:00Z"
    assert row["last_received_snapshot"]["queued_records"] == 1
    assert row["last_received_snapshot"]["scope"] == (
        "last_received_snapshot_not_current_device_state"
    )
    assert service.status(now=START + timedelta(days=1))["devices"][0]["receive_state"] == (
        "not_recently_received"
    )
    assert service.status(now=START)["devices"][0]["receive_state"] == "clock_conflict"
    encoded = json.dumps(result)
    assert "synthetic-device" not in encoded
    assert "synthetic-boot" not in encoded


def test_late_unknown_boot_upload_is_not_called_recent_capture(view, monkeypatch):
    service, payload, _ = view
    monkeypatch.setattr(environment, "_now", lambda: "2026-01-02T00:00:00Z")
    unknown = copy.deepcopy(payload)
    unknown["boot_id"] = "old-unknown-boot"
    unknown["time_anchors"] = []
    unknown["device_status"]["queued_records"] = 20
    add(service, unknown)
    row = service.status(now=START + timedelta(days=1, minutes=1))["devices"][0]
    assert row["receive_state"] == "recently_received"
    assert row["last_mapped_capture"]["utc_end"] == "2026-01-01T00:01:00Z"
    assert row["last_received_snapshot"]["queued_records"] == 20
    assert row["currently_collecting"] is None
