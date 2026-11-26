"""P3 whole-window rendering, privacy, failure isolation, and HTTP boundaries."""

import copy
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.request import OpenerDirector

import pytest
from environment_samples import sample_profile
from fastapi.testclient import TestClient

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.domain.environment_display import reduce_windows
from sleepscape.domain.metrics import metric_definition, metric_registry
from sleepscape.services import environment_dashboard
from sleepscape.services.environment_dashboard import environment_timeline
from sleepscape.services.environment_views import EnvironmentViewService
from sleepscape.services.views import DashboardService
from sleepscape.storage import environment
from sleepscape.storage.environment_index import migrate_index, rebuild_index

START = datetime(2026, 1, 1, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", demo=True)
    payload = json.loads((FIXTURES / "environment-batch.json").read_text())
    profile = sample_profile()
    monkeypatch.setattr(environment, "_now", lambda: "2026-01-02T01:00:00Z")
    monkeypatch.setattr(OpenerDirector, "open", lambda *_args, **_kwargs: pytest.fail("no network"))
    environment.register_local(
        settings.data_dir, payload["device_id"], "synthetic-profile", profile
    )
    environment.ingest(
        settings.data_dir, payload["device_id"], EnvironmentBatch.model_validate(payload)
    )
    migrate_index(settings.data_dir)
    rebuild_index(settings.data_dir)
    return settings, payload, profile


def ingest(fixture, payload):
    settings, _, _ = fixture
    environment.ingest(
        settings.data_dir, payload["device_id"], EnvironmentBatch.model_validate(payload)
    )


def timeline(fixture, start=START, end=START + timedelta(hours=1), points=2000):
    return environment_timeline(fixture[0], start, end, points)


def test_registry_is_unique_public_immutable_and_preserves_original_ids():
    first = metric_registry()
    assert [item["id"] for item in first["items"][:4]] == [
        "heart_rate",
        "hrv",
        "spo2",
        "body_battery",
    ]
    assert len({item["id"] for item in first["items"]}) == 8
    assert "min" not in metric_definition("temperature_c")["axis"]
    assert "min" not in metric_definition("sound_relative")["axis"]
    first["items"][0]["label"] = "mutated"
    assert metric_definition("heart_rate")["label"] == "心率"
    with pytest.raises(ValueError, match="unknown_metric"):
        metric_definition("untrusted")


def test_minute_values_coverage_warming_and_left_edge_window_are_preserved(fixture):
    result = timeline(fixture, START + timedelta(seconds=30), START + timedelta(minutes=1))
    assert len(result["series"]) == 5
    temperature = next(item for item in result["series"] if item["metric"] == "temperature_c")
    point = temperature["points"][0]
    assert point["timestamp"] == point["window_start"] == "2026-01-01T00:00:00Z"
    assert point["window_end"] == "2026-01-01T00:01:00Z"
    assert point["min"] == 21 and point["max"] == 22
    assert point["valid_count"] == point["expected_count"] == 6
    assert point["time_quality"] == "estimated" and point["time_uncertainty_ms"] is None
    sound = next(item for item in result["series"] if item["metric"] == "sound_relative")
    assert sound["unit"] == "dBFS" and sound["points"][0]["value"] == -40
    assert sound["points"][0]["event_detection"]["detected_count"] is None
    assert sound["points"][0]["coverage"]["valid_samples"] == 960000
    assert not result["environment"]["analysis_readiness_evaluated"]
    assert result["environment"]["coverage"]["covered_duration_ms"] == 30000


def test_missing_channel_and_real_gap_do_not_become_zero_or_connected(fixture):
    payload = copy.deepcopy(fixture[1])
    payload["records"][0].update(seq=2, mono_start_ms=120000, mono_end_ms=180000)
    payload["records"][0]["metrics"]["relative_humidity_pct"].update(
        state="missing",
        expected_count=6,
        observed_count=0,
        valid_count=0,
        mean=None,
        min=None,
        max=None,
        quality=["missing"],
    )
    payload["records"][0]["metrics"]["temperature_c"].update(mean=-5.0, min=-6.0, max=-4.0)
    ingest(fixture, payload)
    result = timeline(fixture)
    temperature = next(item for item in result["series"] if item["metric"] == "temperature_c")
    assert [point["value"] for point in temperature["points"]] == [21.5, -5]
    assert len({point["segment_ref"] for point in temperature["points"]}) == 2
    humidity = next(item for item in result["series"] if item["metric"] == "relative_humidity_pct")
    assert humidity["points"][1]["value"] is None
    assert result["environment"]["coverage"]["gaps"][0]["utc_start"] == "2026-01-01T00:01:00Z"
    summary = next(
        item
        for item in result["environment"]["channel_summaries"]
        if item["metric"] == "relative_humidity_pct"
    )
    assert summary["coverage_percent"] == 50 and summary["missing_window_count"] == 1


def test_profile_and_boot_changes_stay_separate_and_identities_never_leak(fixture):
    settings, original, profile = fixture
    environment.add_profile(settings.data_dir, original["device_id"], "secret-profile", profile)
    payload = copy.deepcopy(original)
    payload["boot_id"] = "secret-boot"
    payload["records"][0]["measurement_profile_id"] = "secret-profile"
    ingest(fixture, payload)
    result = timeline(fixture)
    assert len(result["series"]) == 10
    assert len(result["environment"]["sources"]) == 2
    assert result["environment"]["overlaps_require_source_review"]
    public = json.dumps(result)
    for private in ("secret-profile", "secret-boot", original["device_id"], profile.placement_id):
        assert private not in public


def test_event_is_a_bounded_noncontinuous_envelope(fixture):
    payload = copy.deepcopy(fixture[1])
    payload["records"] = [
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
    ingest(fixture, payload)
    event = timeline(fixture)["events"][0]
    assert event["type"] == "environment_sound"
    assert event["metrics"]["active_duration_ms"] == 2000
    assert event["envelope_is_continuous_sound"] is False and event["merged"]


def test_status_distinguishes_late_receipt_capture_and_not_live_state(fixture):
    service = DashboardService(fixture[0])
    service._now = lambda: datetime(2026, 1, 2, 1, 1, tzinfo=UTC)
    result = service.status()
    assert result["capabilities"][-1]["available"]
    device = result["environment"]["devices"][0]
    assert device["receive_state"] == "recently_received"
    assert device["last_mapped_capture"]["utc_end"] == "2026-01-01T00:01:00Z"
    assert device["currently_collecting"] is None
    assert device["last_measurement_profile"]["firmware_version"] == "0.4.0"
    assert device["channel_health_scope"] == "last_mapped_minute_not_live"
    assert device["channel_health"]["temperature_c"]["valid_count"] == 6


def test_no_garmin_night_uses_environment_without_inventing_sleep(fixture):
    service = DashboardService(fixture[0])
    night = service.night(date(2026, 1, 1))
    assert night["environment_only"] and not night["garmin_sleep_available"]
    assert night["sleep"]["start_at"] is None
    assert night["cards"]["average_heart_rate"]["value"] is None
    result = service.timeline(date(2026, 1, 1))
    assert len([item for item in result["series"] if item["source"] == "garmin"]) == 4
    assert len([item for item in result["series"] if item["source"] == "environment"]) == 5
    assert result["stages"] == []


def test_custom_environment_window_does_not_require_a_sleep_date(fixture):
    service = DashboardService(fixture[0])
    assert service.night(date(2026, 1, 5)) is None
    result = service.timeline(date(2026, 1, 5), start=START, end=START + timedelta(hours=1))
    assert result["environment"]["record_count"] == 1
    assert result["quality"] == ["sleep_window_unavailable"]


def test_environment_failure_isolated_from_garmin_status_and_explicit_timeline(
    fixture, monkeypatch
):
    def failure(*args, **kwargs):
        raise RuntimeError("PRIVATE_DATA_MUST_NOT_APPEAR")

    monkeypatch.setattr(EnvironmentViewService, "timeline", failure)
    monkeypatch.setattr(EnvironmentViewService, "status", failure)
    service = DashboardService(fixture[0])
    assert service.status()["environment"]["state"] == "unavailable"
    result = service.timeline(date(2026, 1, 1), start=START, end=START + timedelta(hours=1))
    assert result["environment"]["state"] == "unavailable"
    assert "PRIVATE_DATA" not in json.dumps(result)


def test_gets_are_readonly_and_protected_and_invalid_ranges_rejected(fixture):
    settings = fixture[0]
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
        before = db.execute("SELECT COUNT(*) FROM environment_records").fetchone()[0]
    url = "/api/v1/environment/timeline?from=2026-01-01T00:00:00Z&to=2026-01-01T01:00:00Z"
    with TestClient(
        create_app(settings), base_url="http://localhost", client=("127.0.0.1", 5000)
    ) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert len(response.json()["series"]) == 5
        assert client.get("/api/v1/metrics").json() == metric_registry()
        assert client.post("/api/v1/metrics").status_code == 405
        assert client.get(url.replace("01:00:00Z", "01:00:00")).status_code == 422
        assert client.get(url.replace("01-01T01", "01-04T01")).status_code == 422
        assert client.get(url + "&max_points=2001").status_code == 422
    with TestClient(
        create_app(settings), base_url="http://localhost", client=("192.0.2.3", 5000)
    ) as client:
        assert client.get(url).status_code == 403
        assert client.get("/api/v1/metrics").status_code == 403
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM environment_records").fetchone()[0] == before


def test_window_reduction_retains_observed_extremes_and_segment_boundaries():
    points = [
        {
            "window_start": (START + timedelta(minutes=i)).isoformat(),
            "window_end": (START + timedelta(minutes=i + 1)).isoformat(),
            "timestamp": (START + timedelta(minutes=i)).isoformat(),
            "record_ref": f"r{i}",
            "segment_ref": f"segment{i // 5}",
            "value": 10.0,
            "min": -50.0 if i == 2 else 9.0,
            "max": 80.0 if i == 8 else 11.0,
        }
        for i in range(10)
    ]
    result = reduce_windows(points, START, START + timedelta(minutes=10), 2)
    assert [p["record_ref"] for p in result["points"]] == ["r2", "r8"]
    assert [p["segment_ref"] for p in result["points"]] == ["segment0", "segment1"]
    assert result["display_resolution"]["method"] == "window_minmax"


def test_raw_record_limit_never_claims_complete_coverage(fixture, monkeypatch):
    payload = copy.deepcopy(fixture[1])
    payload["records"][0].update(seq=1, mono_start_ms=60000, mono_end_ms=120000)
    ingest(fixture, payload)
    original = EnvironmentViewService.timeline
    monkeypatch.setattr(
        EnvironmentViewService,
        "timeline",
        lambda self, start, end: original(self, start, end, limit=1),
    )
    result = timeline(fixture)["environment"]
    assert result["truncated"] and result["record_count"] == 1
    assert result["coverage"] is None
    assert result["segments"][0]["coverage"] is None
    assert result["summary_scope"] == "returned_original_windows_not_sleep_completeness"


def test_source_and_event_limits_are_explicit_and_isolated(fixture, monkeypatch):
    monkeypatch.setattr(environment_dashboard, "MAX_GROUPS", 1)
    monkeypatch.setattr(environment_dashboard, "MAX_EVENTS", 1)
    second = copy.deepcopy(fixture[1])
    second["boot_id"] = "second-boot"
    ingest(fixture, second)
    payload = copy.deepcopy(fixture[1])
    payload["records"] = [
        {
            "record_type": "event",
            "seq": seq,
            "measurement_profile_id": "synthetic-profile",
            "mono_start_ms": seq * 10000,
            "mono_end_ms": seq * 10000 + 1000,
            "quality": [],
            "active_duration_ms": 1000,
            "max_1s_rms_dbfs": -30.0,
            "baseline_dbfs": -45.0,
            "threshold_profile_id": "relative-sound-60s-10db-6db",
            "merged": False,
            "incomplete": False,
        }
        for seq in range(2)
    ]
    ingest(fixture, payload)
    result = timeline(fixture)
    assert len(result["series"]) == 5 and len(result["events"]) == 1
    assert result["environment"]["series_truncated"]
    assert result["environment"]["events_truncated"]
    assert result["environment"]["record_count"] == 4
    assert result["environment"]["displayed_minute_count"] == 1


def test_group_limit_keeps_latest_full_night_and_older_groups_remain_queryable(fixture):
    for minute in range(1, 9):
        payload = copy.deepcopy(fixture[1])
        payload["boot_id"] = f"early-restart-{minute}"
        payload["time_anchors"][0]["utc_at"] = (
            (START + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
        )
        payload["device_status"] = None
        ingest(fixture, payload)

    for first in range(0, 480, 60):
        payload = copy.deepcopy(fixture[1])
        payload["boot_id"] = "current-night"
        payload["time_anchors"][0]["utc_at"] = "2026-01-01T00:09:00Z"
        payload["device_status"] = None
        original = payload["records"][0]
        payload["records"] = [
            {
                **copy.deepcopy(original),
                "seq": minute,
                "mono_start_ms": minute * 60000,
                "mono_end_ms": (minute + 1) * 60000,
            }
            for minute in range(first, first + 60)
        ]
        ingest(fixture, payload)

    result = timeline(fixture, end=START + timedelta(hours=9))
    metadata = result["environment"]
    assert metadata["record_count"] == 489
    assert metadata["displayed_minute_count"] == 487
    assert metadata["series_truncated"] and not metadata["truncated"]
    assert len(metadata["segments"]) == 8 and len(result["series"]) == 40
    temperatures = [item for item in result["series"] if item["metric"] == "temperature_c"]
    assert [item["points"][0]["window_start"] for item in temperatures] == sorted(
        item["points"][0]["window_start"] for item in temperatures
    )
    current = temperatures[-1]["points"]
    assert len(current) == 480
    assert current[0]["window_start"] == "2026-01-01T00:09:00Z"
    assert current[-1]["window_end"] == "2026-01-01T08:09:00Z"
    assert len({point["segment_ref"] for point in current}) == 1

    older = timeline(fixture, end=START + timedelta(minutes=1))
    assert older["environment"]["record_count"] == 1
    assert older["environment"]["displayed_minute_count"] == 1
    assert not older["environment"]["series_truncated"]
    assert older["series"][0]["points"][0]["window_start"] == "2026-01-01T00:00:00Z"


def test_group_limit_uses_last_capture_not_first_capture_or_late_receipt(fixture, monkeypatch):
    monkeypatch.setattr(environment_dashboard, "MAX_GROUPS", 1)
    payload = copy.deepcopy(fixture[1])
    payload["records"][0].update(seq=2, mono_start_ms=120000, mono_end_ms=180000)
    ingest(fixture, payload)
    monkeypatch.setattr(environment, "_now", lambda: "2026-01-03T01:00:00Z")
    older = copy.deepcopy(fixture[1])
    older["boot_id"] = "late-upload"
    older["time_anchors"][0]["utc_at"] = "2026-01-01T00:01:00Z"
    ingest(fixture, older)

    result = timeline(fixture)
    points = result["series"][0]["points"]
    assert result["environment"]["record_count"] == 3
    assert result["environment"]["displayed_minute_count"] == 2
    assert [point["window_start"] for point in points] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:02:00Z",
    ]
    assert len({point["segment_ref"] for point in points}) == 2


@pytest.mark.parametrize("offset_ms", [0, 500])
def test_group_limit_handles_fractional_seconds_and_stable_ties(fixture, monkeypatch, offset_ms):
    monkeypatch.setattr(environment_dashboard, "MAX_GROUPS", 1)
    payload = copy.deepcopy(fixture[1])
    payload["boot_id"] = "another-boot"
    payload["time_anchors"][0]["utc_at"] = (
        (START + timedelta(milliseconds=offset_ms)).isoformat().replace("+00:00", "Z")
    )
    ingest(fixture, payload)
    view = EnvironmentViewService(fixture[0]).timeline(START, START + timedelta(hours=1))
    expected_latest = view["records"][-1]["boot_ref"]

    first = timeline(fixture)
    view["records"].reverse()
    monkeypatch.setattr(
        EnvironmentViewService, "timeline", lambda *_args, **_kwargs: copy.deepcopy(view)
    )
    reversed_input = timeline(fixture)
    assert [item["id"] for item in first["series"]] == [
        item["id"] for item in reversed_input["series"]
    ]
    if offset_ms:
        assert all(item["boot_ref"] == expected_latest for item in first["series"])


def test_unknown_and_conflict_time_remain_global_and_never_plot(fixture):
    unknown = copy.deepcopy(fixture[1])
    unknown.update(boot_id="unanchored", time_anchors=[], device_status=None)
    ingest(fixture, unknown)
    conflict = copy.deepcopy(fixture[1])
    conflict["boot_id"] = "clock-jump"
    second = copy.deepcopy(conflict["time_anchors"][0])
    second.update(anchor_id="jump", monotonic_ms=60000, utc_at="2026-01-01T00:02:00Z")
    conflict["time_anchors"].append(second)
    ingest(fixture, conflict)
    result = timeline(fixture)
    assert result["environment"]["record_count"] == 1
    assert len(result["series"]) == 5
    counts = result["environment"]["global_time_state"]
    assert counts["unknown_record_count"] == counts["conflict_record_count"] == 1
    assert counts["scope"] == "all_history_not_window"


def test_unindexed_reads_never_initialize_index_or_expose_raw_profiles(tmp_path, fixture):
    settings, payload, profile = fixture
    fresh = Settings(_env_file=None, data_dir=tmp_path / "unindexed", demo=True)
    environment.register_local(fresh.data_dir, payload["device_id"], "synthetic-profile", profile)
    environment.ingest(
        fresh.data_dir, payload["device_id"], EnvironmentBatch.model_validate(payload)
    )
    path = fresh.data_dir / "sleepscape.sqlite3"
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    result = environment_timeline(fresh, START, START + timedelta(hours=1), 2000)
    assert result["environment"]["state"] == "index_not_initialized"
    assert result["series"] == [] and result["environment"]["coverage"] is None
    assert DashboardService(fresh).status()["environment"]["state"] == "index_not_initialized"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == before

    private = "PRIVATE-untrusted-configuration"
    values = profile.model_dump(mode="json")
    for name in (
        "hardware_model",
        "firmware_version",
        "temperature_sensor",
        "light_sensor",
        "microphone",
        "sound_processing",
        "placement_id",
        "calibration_id",
    ):
        values[name] = private
    unknown_profile = MeasurementProfile.model_validate(values)
    environment.add_profile(settings.data_dir, payload["device_id"], private, unknown_profile)
    second = copy.deepcopy(payload)
    second["boot_id"] = "profile-unknown"
    second["records"][0].update(
        measurement_profile_id=private, seq=2, mono_start_ms=120000, mono_end_ms=180000
    )
    ingest(fixture, second)
    public = json.dumps(
        {"timeline": timeline(fixture), "status": DashboardService(settings).status()}
    )
    assert private not in public
    assert (
        DashboardService(settings).status()["environment"]["devices"][0][
            "last_measurement_profile"
        ]["hardware_model"]
        == "unknown"
    )
