"""Dual-source integration uses stored synthetic observations, never live APIs or model calls."""

import copy
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.request import OpenerDirector

import pytest
from fastapi.testclient import TestClient

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.services import readiness
from sleepscape.services.analysis_views import analysis_view
from sleepscape.services.environment_views import EnvironmentViewService
from sleepscape.services.views import DashboardService
from sleepscape.storage import environment
from sleepscape.storage.analysis import AnalysisStorageError, read_latest
from sleepscape.storage.environment_index import migrate_index, rebuild_index
from sleepscape.storage.reader import DashboardReader
from sleepscape.storage.repository import Repository

DAY = date(2026, 1, 1)
NOW = datetime(2026, 1, 1, 1, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(OpenerDirector, "open", lambda *a, **k: pytest.fail("no external requests"))


@pytest.fixture
def data(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        llm_config_file=Path(__file__).resolve().parents[1] / "configs/models.toml",
    )
    with Repository(settings.data_dir) as repo:
        run = repo.start_run({})

        def store(resource, day, records, metrics=None):
            req = DataRequest(resource, day, "/synthetic", start_date=day, end_date=day)
            rows = [
                {
                    "kind": kind,
                    "key": day,
                    "date": day,
                    "timestamp": None,
                    "data": {"quality": {}, **values},
                }
                for kind, values in records
            ]
            repo.save_snapshot(
                run,
                FetchResult(req, {"synthetic": True}, NOW.isoformat(), 200),
                NormalizedBatch(rows, metrics=metrics or {}),
            )

        store(
            "sleep",
            str(DAY),
            [
                (
                    "sleep",
                    {
                        "start_at": "2026-01-01T00:00:00Z",
                        "end_at": "2026-01-01T00:02:00Z",
                        "sleep_seconds": 120,
                        "average_heart_rate": 55,
                        "confirmed": True,
                        "stage_timeline_available": False,
                    },
                )
            ],
        )
        store("hrv", str(DAY), [("hrv_daily", {"night_average_ms": 50})])
        store("activities", "2025-12-31", [], {"pagination_complete": True})
        store("daily_summaries", "2025-12-31", [("daily_summary", {"average_stress": 0})])
        repo.finish_run(run, "completed", {})
    batch = json.loads((FIXTURES / "environment-batch.json").read_text())
    profile = MeasurementProfile.model_validate_json(
        (FIXTURES / "environment-profile.json").read_bytes()
    )
    environment.register_local(settings.data_dir, batch["device_id"], "synthetic-profile", profile)
    environment.ingest(
        settings.data_dir, batch["device_id"], EnvironmentBatch.model_validate(batch)
    )
    migrate_index(settings.data_dir)
    rebuild_index(settings.data_dir)
    return settings, batch


def complete(data):
    settings, original = data
    second = copy.deepcopy(original)
    second["records"][0].update(seq=1, mono_start_ms=60000, mono_end_ms=120000)
    environment.ingest(
        settings.data_dir, second["device_id"], EnvironmentBatch.model_validate(second)
    )


def test_check_and_api_do_not_initialize_storage_or_call_model(data):
    settings, _ = data
    before = (settings.data_dir / "sleepscape.sqlite3").read_bytes()
    result = readiness.check_readiness(settings, day=DAY, now=NOW, persist=False)
    assert result["status"] == "checked" and result["model_calls"] == 0
    assert result["results"][0]["data_state"] == "waiting_data"
    assert result["results"][0]["core_ready"] is False
    with DashboardReader(settings.data_dir) as reader:
        assert not reader._query("SELECT name FROM sqlite_master WHERE name='analysis_schema'")
    assert (settings.data_dir / "sleepscape.sqlite3").read_bytes() == before
    public = json.dumps(result)
    for secret in ("source_fingerprint", "synthetic-device", "synthetic-profile", "device_id"):
        assert secret not in public
    with pytest.raises(AnalysisStorageError):
        readiness.check_readiness(settings, day=DAY, now=NOW)


def test_explicit_prepare_checks_backup_and_late_upload_changes_readiness(data):
    settings, _ = data
    assert readiness.prepare_readiness(settings)["backup_verified"] is True
    assert readiness.prepare_readiness(settings)["status"] == "already_initialized"
    first = readiness.check_readiness(settings, day=DAY, now=NOW)
    assert first["counts"]["waiting_data"] == 1
    saved = read_latest(settings.data_dir, DAY.isoformat())
    complete(data)
    second = readiness.check_readiness(settings, day=DAY, now=NOW + timedelta(minutes=1))
    assert second["counts"]["ready"] == 1
    current = read_latest(settings.data_dir, DAY.isoformat())
    assert current["source_fingerprint"] != saved["source_fingerprint"]
    assert current["core_ready"] and not current["missing"]
    assert current["checked_at"] != saved["checked_at"]
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM analysis_readiness_checks").fetchone()[0] == 2
    view = DashboardService(settings)
    view._now = lambda: NOW
    summary = view.nights(DAY, DAY)["items"][0]["analysis_summary"]
    assert summary["state"] == "ready" and summary["scope"] == "last_saved_check"


def test_evaluation_is_one_sqlite_snapshot_while_hardware_commits(data, monkeypatch):
    settings, _ = data
    original = EnvironmentViewService.timeline
    invoked = []

    def during_read(self, start, end, **kwargs):
        assert kwargs["reader"].connection.in_transaction
        if not invoked:
            invoked.append(True)
            complete(data)
        return original(self, start, end, **kwargs)

    monkeypatch.setattr(EnvironmentViewService, "timeline", during_read)
    before = readiness.evaluate_night(settings, DAY, now=NOW)
    after = readiness.evaluate_night(settings, DAY, now=NOW)
    assert not before["core_ready"] and after["core_ready"]
    assert before["source_fingerprint"] != after["source_fingerprint"]


def test_fingerprint_ignores_check_clock_and_mapping_rebuild(data):
    settings, _ = data
    complete(data)
    first = readiness.evaluate_night(settings, DAY, now=NOW)
    rebuild_index(settings.data_dir, force=True)
    second = readiness.evaluate_night(settings, DAY, now=NOW + timedelta(minutes=5))
    assert first["source_fingerprint"] == second["source_fingerprint"]
    assert first["checked_at"] != second["checked_at"]


def test_readiness_is_independent_of_model_configuration_and_refresh_does_not_save(
    data, monkeypatch
):
    settings, _ = data
    complete(data)
    original = readiness.evaluate_night
    monkeypatch.setattr(
        "sleepscape.services.analysis_views.evaluate_night",
        lambda settings, day: original(settings, day, now=NOW),
    )
    result = analysis_view(settings, DAY)
    assert result["implementation_stage"] == "reports"
    assert result["data_state"] == "ready" and result["execution_state"] == "not_initialized"
    assert result["report"] is None and result["readiness"]["core_ready"]
    assert result["blockers"] == ["sleep_analysis_not_initialized"]
    assert read_latest(settings.data_dir, DAY.isoformat()) is None
    with TestClient(
        create_app(settings), base_url="http://localhost", client=("127.0.0.1", 5000)
    ) as client:
        response = client.get(f"/api/v1/nights/{DAY}/analysis")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert client.post(f"/api/v1/nights/{DAY}/analysis").status_code == 405
        assert "source_fingerprint" not in response.text
    assert read_latest(settings.data_dir, DAY.isoformat()) is None


def test_empty_readonly_service_does_not_create_directories(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "absent")
    result = readiness.check_readiness(settings, day=DAY, now=NOW, persist=False)
    assert result["results"][0]["data_state"] == "waiting_data"
    assert not settings.data_dir.exists()


def test_days_outside_window_preserve_actual_readiness(data):
    settings, _ = data
    complete(data)
    result = readiness.evaluate_night(settings, DAY, now=NOW + timedelta(days=7))
    assert result["data_state"] == "expired_unanalyzed"
    assert result["core_ready"] is True


def test_daily_checks_configured_window_after_releasing_collection_lock(data, monkeypatch):
    from sleepscape.services import daily
    from sleepscape.storage.daily_runtime import collection_busy

    settings, _ = data
    complete(data)
    readiness.prepare_readiness(settings)
    original = readiness.check_readiness

    def collect(*args, **kwargs):
        assert collection_busy(settings.data_dir)
        return {"status": "completed", "completed_resources": 51, "failed_resources": 0}

    def check(*args, **kwargs):
        assert not collection_busy(settings.data_dir)
        return original(*args, **kwargs)

    monkeypatch.setattr(daily, "collect", collect)
    monkeypatch.setattr(readiness, "check_readiness", check)
    result = daily.run_daily(settings, now=NOW)
    assert result["status"] == "completed" and result["analysis"] == "disabled"
    assert result["readiness"]["counts"] == {"ready": 1, "waiting_data": 6, "expired_unanalyzed": 0}
    assert result["readiness"]["window"] == {"start_date": "2025-12-26", "end_date": "2026-01-01"}
    assert read_latest(settings.data_dir, str(DAY))["core_ready"]


def test_daily_readiness_failure_does_not_erase_successful_collection(data, monkeypatch):
    from sleepscape.services import daily

    settings, _ = data

    def failed(*args, **kwargs):
        raise RuntimeError("PRIVATE_MUST_NOT_LEAK")

    monkeypatch.setattr(daily, "collect", lambda *a, **kw: {"status": "completed"})
    monkeypatch.setattr(readiness, "check_readiness", failed)
    result = daily.run_daily(settings, now=NOW)
    assert result["status"] == "completed"
    assert result["readiness"] == {"status": "failed", "code": "analysis_readiness_unavailable"}
    assert "PRIVATE_MUST_NOT_LEAK" not in json.dumps(result)


def test_optional_samples_are_detected_without_inventing_missing_cards(data):
    settings, _ = data
    with Repository(settings.data_dir) as repo:
        run = repo.start_run({})
        for resource, kind, field in [
            ("spo2", "spo2_sample", "percent"),
            ("body_battery", "body_battery_sample", "level"),
        ]:
            req = DataRequest(
                resource, str(DAY), "/synthetic", start_date=str(DAY), end_date=str(DAY)
            )
            record = {
                "kind": kind,
                "key": "synthetic",
                "date": str(DAY),
                "timestamp": "2026-01-01T00:00:30Z",
                "data": {field: 98},
            }
            repo.save_snapshot(
                run, FetchResult(req, {}, NOW.isoformat(), 200), NormalizedBatch([record])
            )
        repo.finish_run(run, "completed", {})
    result = readiness.evaluate_night(settings, DAY, now=NOW)
    missing = {row["metric"] for row in result["optional_missing"]}
    assert "sleep.spo2" not in missing and "sleep.body_battery" not in missing
    assert "previous.activities" not in missing


def test_prepare_accepts_default_relative_data_directory(data, monkeypatch):
    settings, _ = data
    monkeypatch.chdir(settings.data_dir.parent)
    settings.data_dir = Path("data")
    assert readiness.prepare_readiness(settings)["backup_verified"]
    assert readiness.check_readiness(settings, day=DAY, now=NOW)["persisted"]


def _optional_samples(settings, samples):
    with Repository(settings.data_dir) as repo:
        run = repo.start_run({})
        req = DataRequest("spo2", str(DAY), "/synthetic", start_date=str(DAY), end_date=str(DAY))
        records = [
            {
                "kind": "spo2_sample",
                "key": stamp,
                "date": str(DAY),
                "timestamp": stamp,
                "data": {"percent": 98, "source_plottable": valid},
            }
            for stamp, valid in samples
        ]
        repo.save_snapshot(
            run, FetchResult(req, {}, NOW.isoformat(), 200), NormalizedBatch(records)
        )
        repo.finish_run(run, "completed", {})


def test_optional_sample_at_sleep_end_is_outside_half_open_window(data):
    settings, _ = data
    _optional_samples(settings, [("2026-01-01T00:02:00Z", True)])
    result = readiness.evaluate_night(settings, DAY, now=NOW)
    assert "sleep.spo2" in {row["metric"] for row in result["optional_missing"]}
    with DashboardReader(settings.data_dir) as reader:
        view = DashboardService(settings)
        view._now = lambda: NOW
        assert view.readiness_night(reader, DAY)["optional_observations"] == []


def test_optional_validity_change_changes_fingerprint_even_if_available(data):
    settings, _ = data
    samples = [("2026-01-01T00:00:30Z", True), ("2026-01-01T00:01:30Z", True)]
    _optional_samples(settings, samples)
    first = readiness.evaluate_night(settings, DAY, now=NOW)
    samples[1] = (samples[1][0], False)
    _optional_samples(settings, samples)
    second = readiness.evaluate_night(settings, DAY, now=NOW)
    assert first["optional_missing"] == second["optional_missing"]
    assert first["source_fingerprint"] != second["source_fingerprint"]


def add_partial_sound_minute(data, *, missing_seconds=1):
    settings, original = data
    second = copy.deepcopy(original)
    second["records"][0].update(seq=1, mono_start_ms=60000, mono_end_ms=120000)
    sound = second["records"][0]["metrics"]["sound_relative"]
    sound.update(
        state="partial",
        valid_count=60 - missing_seconds,
        valid_1s_count=60 - missing_seconds,
        valid_duration_ms=(60 - missing_seconds) * 1000,
        quality=["partial_window", "audio_gap", "sensor_error"],
    )
    # Raw PCM counts can be full while one second fails quality/timing checks.
    environment.ingest(
        settings.data_dir, second["device_id"], EnvironmentBatch.model_validate(second)
    )


def test_small_gap_is_saved_and_shown_as_eligible_without_hiding_missing_samples(data, monkeypatch):
    settings, _ = data
    add_partial_sound_minute(data)
    readiness.prepare_readiness(settings)
    result = readiness.check_readiness(settings, day=DAY, now=NOW)
    observed = result["results"][0]
    assert observed["data_state"] == "ready"
    assert observed["quality_state"] == "with_gaps"
    assert observed["core_ready"] and observed["missing"] == [] and observed["warnings"]
    sound = observed["environment_summary"]["channels"]["sound_relative"]
    assert sound["effective_coverage_ratio"] == pytest.approx(119 / 120)
    assert sound["usable_count"] == 119
    assert sound["valid_windows"] == 1
    assert sound["audio_missing_seconds"] == 1
    assert sound["max_affected_span_seconds"] == 60
    assert result["model_calls"] == 0
    saved = read_latest(settings.data_dir, str(DAY))
    assert saved["warnings"] == observed["warnings"]
    assert saved["policy"] == {"min_channel_coverage": 0.99, "max_affected_span_seconds": 300}
    service = DashboardService(settings)
    service._now = lambda: NOW
    summary = service.nights(DAY, DAY)["items"][0]["analysis_summary"]
    assert summary["quality_state"] == "with_gaps" and summary["current_policy"] is True
    original = readiness.evaluate_night
    monkeypatch.setattr(
        "sleepscape.services.analysis_views.evaluate_night",
        lambda s, day: original(s, day, now=NOW),
    )
    response = analysis_view(settings, DAY)
    assert response["readiness"]["warnings"] == observed["warnings"]
    assert response["report"] is None and response["execution_state"] == "not_initialized"
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM analysis_readiness_checks").fetchone()[0] == 1


def test_below_threshold_remains_blocked_and_saves_actual_gap_details(data):
    settings, _ = data
    add_partial_sound_minute(data, missing_seconds=3)
    readiness.prepare_readiness(settings)
    result = readiness.check_readiness(settings, day=DAY, now=NOW)["results"][0]
    assert not result["core_ready"] and result["quality_state"] == "insufficient"
    assert any(item["code"] == "invalid_channel" for item in result["missing"])
    assert "insufficient_channel_coverage" in {x["code"] for x in result["missing"]}
    sound = result["environment_summary"]["channels"]["sound_relative"]
    assert sound["effective_coverage_ratio"] == pytest.approx(117 / 120)


def test_legacy_audit_is_preserved_and_history_names_old_policy_until_rechecked(data):
    from uuid import uuid4

    from test_analysis_storage import ready as archived_snapshot

    from sleepscape.storage.analysis import save_results

    settings, _ = data
    add_partial_sound_minute(data)
    readiness.prepare_readiness(settings)
    old = archived_snapshot(str(DAY), "2026-01-01T00:03:00Z")
    old["window"] = {
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T00:02:00Z",
        "duration_seconds": 120,
    }
    save_results(settings.data_dir, [old], run_id=str(uuid4()))
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
        before = db.execute("SELECT * FROM analysis_readiness_checks ORDER BY check_id").fetchone()
    service = DashboardService(settings)
    service._now = lambda: NOW
    summary = service.nights(DAY, DAY)["items"][0]["analysis_summary"]
    assert summary["policy_version"] == "dual-source-strict-1"
    assert summary["quality_state"] is None and summary["current_policy"] is False
    readiness.check_readiness(settings, day=DAY, now=NOW)
    summary = service.nights(DAY, DAY)["items"][0]["analysis_summary"]
    assert summary["quality_state"] == "with_gaps" and summary["current_policy"] is True
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
        rows = db.execute("SELECT * FROM analysis_readiness_checks ORDER BY check_id").fetchall()
        assert len(rows) == 2 and rows[0] == before


def test_fingerprint_tracks_policy_parameters_without_changing_source_facts(data, monkeypatch):
    settings, _ = data
    complete(data)
    first = readiness.evaluate_night(settings, DAY, now=NOW)
    evaluate = readiness.evaluate_readiness

    def changed(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        result["policy"] = {**result["policy"], "max_affected_span_seconds": 240}
        return result

    monkeypatch.setattr(readiness, "evaluate_readiness", changed)
    second = readiness.evaluate_night(settings, DAY, now=NOW)
    assert first["source_fingerprint"] != second["source_fingerprint"]
    assert first["environment_summary"] == second["environment_summary"]
