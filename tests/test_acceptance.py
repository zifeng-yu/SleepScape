"""Disposable synthetic source databases; the audit never authenticates or changes the source."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
import requests

from sleepscape.config import Settings
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.domain.normalize import normalize
from sleepscape.services import acceptance
from sleepscape.storage import Repository

PRIVATE_MARKER = "SYNTHETIC_PRIVATE_PAYLOAD_NOT_FOR_REPORT"


@pytest.fixture(autouse=True)
def no_network_or_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline acceptance must not access network or credential text files")

    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)


@pytest.fixture
def populated_source(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_PERSONAL_API_BASE_URL", raising=False)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "source",
        timezone="Asia/Shanghai",
        garmin_key_file=tmp_path / "do-not-read-key",
        garmin_token_cache=tmp_path / "do-not-read-token",
    )
    with Repository(settings.data_dir) as repository:
        run = repository.start_run({"planned_requests": 2, "timezone": "Asia/Shanghai"})
        sleep_request = DataRequest(
            "sleep", "2026-01-02", "/synthetic", start_date="2026-01-02", end_date="2026-01-02"
        )
        sleep_payload = {
            "calendarDate": "2026-01-02",
            "sleepTimeSeconds": 60,
            "sleepStartTimestampGMT": "2026-01-01T15:00:00Z",
            "sleepEndTimestampGMT": "2026-01-01T15:01:00Z",
            "synthetic_unmapped_field": PRIVATE_MARKER,
        }
        repository.save_snapshot(
            run,
            FetchResult(sleep_request, sleep_payload, "2026-01-03T00:00:00Z", 200),
            normalize(sleep_request, sleep_payload),
        )
        activities_request = DataRequest(
            "activities",
            "2026-01-01:2026-01-02",
            "/synthetic",
            start_date="2026-01-01",
            end_date="2026-01-02",
        )
        activity = {
            "activityId": 987654321,
            "startTimeGMT": "2026-01-02T01:00:00Z",
            "elapsedDuration": 60,
            "activityTrainingLoad": 12,
        }
        page_request = replace(activities_request, resource="activities_page", key="synthetic-page")
        repository.save_snapshot(
            run,
            FetchResult(page_request, [activity], "2026-01-03T00:00:00Z", 200),
            NormalizedBatch(
                metrics={"raw_only": True, "item_count": 1, "payload_shape_valid": True}
            ),
        )
        payload = {"pagination_complete": True, "pages": [[activity]]}
        repository.save_snapshot(
            run,
            FetchResult(activities_request, payload, "2026-01-03T00:00:00Z", 200),
            normalize(activities_request, payload),
        )
        repository.finish_run(run, "completed", {})
        yield settings, repository


def test_source_replay_and_synthetic_safety_are_separate_and_source_is_unchanged(
    populated_source, monkeypatch
):
    settings, source = populated_source
    before_counts = source.counts()
    before_records = source.list_records()
    before_requests = source.list_requests()
    source_bytes = source.path.read_bytes()
    real_connect = acceptance.sqlite3.connect
    connections = []

    def checked_connect(database, *args, **kwargs):
        if str(settings.data_dir) in str(database):
            assert str(database).endswith("?mode=ro")
            assert kwargs["uri"] is True
            connections.append(database)
        return real_connect(database, *args, **kwargs)

    class ShadowOnlyRepository(Repository):
        def __init__(self, directory):
            assert Path(directory).resolve() != settings.data_dir.resolve()
            super().__init__(directory)

    monkeypatch.setattr(acceptance.sqlite3, "connect", checked_connect)
    monkeypatch.setattr(acceptance, "Repository", ShadowOnlyRepository)
    report = acceptance.audit(settings)
    assert report["status"] == "passed"
    assert len(connections) == 1
    assert report["source"]["latest_run_complete"] is True
    assert report["source"]["latest_run_fresh"] is True
    replay = report["data_replay"]
    assert replay["successful_requests_replayed"] == 3
    assert replay["raw_only_requests"] == 1
    assert replay["source_unique_snapshots"] == replay["repeat_import_snapshots"] == 3
    assert replay["source_scoped_records"] == replay["first_import_records"] == 2
    assert replay["all_normalized_records_match"] is True
    assert replay["repeat_current_records_stable"] is True
    assert all(report["synthetic_checks"].values())
    assert report["temporary_files_private"] is True
    assert report["temporary_files_removed"] is True
    assert source.counts() == before_counts
    assert source.list_records() == before_records
    assert source.list_requests() == before_requests
    assert source.path.read_bytes() == source_bytes
    serialized = json.dumps(report, allow_nan=False)
    for private in (PRIVATE_MARKER, "987654321", source.latest_run()["id"], str(settings.data_dir)):
        assert private not in serialized


def test_latest_incomplete_run_cannot_be_hidden_by_earlier_complete_run(populated_source):
    settings, source = populated_source
    run = source.start_run({"planned_requests": 1})
    source.record_failure(
        run, DataRequest("sleep", "synthetic-failure", "/unused"), "synthetic_error"
    )
    source.finish_run(run, "partial", {})
    report = acceptance.audit(settings)
    assert report["status"] == "needs_attention"
    assert report["source"]["latest_complete_run_is_latest"] is False
    assert report["source"]["latest_run_status"] == "partial"
    assert report["data_replay"]["performed"] is True
    assert report["data_replay"]["all_normalized_records_match"] is True


def test_normalized_source_corruption_is_detected_without_returning_values(populated_source):
    settings, source = populated_source
    with source._connection:
        source._connection.execute(
            "UPDATE normalized_records SET data_json = ? WHERE resource = 'sleep'",
            (json.dumps({"synthetic_changed_value": PRIVATE_MARKER}),),
        )
    report = acceptance.audit(settings)
    assert report["status"] == "needs_attention"
    assert report["data_replay"]["source_record_mismatches"] == 1
    assert report["data_replay"]["normalization_content_mismatches"] == 1
    assert report["data_replay"]["provenance_metadata_mismatches"] == 0
    assert report["data_replay"]["all_normalized_records_match"] is False
    assert PRIVATE_MARKER not in json.dumps(report)


def test_raw_hash_corruption_stops_replay_with_static_error(populated_source):
    settings, source = populated_source
    with source._connection:
        source._connection.execute(
            "UPDATE raw_snapshots SET content_hash = ? WHERE resource = 'sleep'", (PRIVATE_MARKER,)
        )
    report = acceptance.audit(settings)
    assert report["status"] == "needs_attention"
    assert report["source"]["raw_hash_or_json_failures"] == 1
    assert report["data_replay"]["performed"] is False
    assert report["errors"] == ["raw_snapshot_integrity_failed"]
    assert PRIVATE_MARKER not in json.dumps(report)


def test_old_completed_run_is_replayable_but_not_claimed_fresh(populated_source):
    settings, source = populated_source
    with source._connection:
        source._connection.execute("UPDATE runs SET finished_at = '2000-01-01T00:00:00Z'")
    report = acceptance.audit(settings)
    assert report["status"] == "needs_attention"
    assert report["source"]["latest_run_fresh"] is False
    assert report["source"]["latest_run_age_seconds"] > report["source"]["freshness_limit_seconds"]
    assert report["data_replay"]["all_normalized_records_match"] is True


def test_completed_label_does_not_replace_planned_request_completeness(populated_source):
    settings, source = populated_source
    with source._connection:
        source._connection.execute(
            "UPDATE runs SET plan_json = ?", (json.dumps({"planned_requests": 5}),)
        )
    report = acceptance.audit(settings)
    assert report["status"] == "needs_attention"
    assert report["source"]["selected_run_plan_fully_recorded"] is False


def test_missing_source_is_not_created_and_still_reports_synthetic_checks(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "absent")
    report = acceptance.audit(settings)
    assert not settings.data_dir.exists()
    assert report["status"] == "needs_attention"
    assert report["errors"] == ["source_database_missing"]
    assert report["data_replay"]["performed"] is False
    assert all(report["synthetic_checks"].values())


def test_unexpected_exception_text_is_not_exposed(populated_source, monkeypatch):
    settings, _ = populated_source

    def broken_normalizer(*args):
        raise RuntimeError(PRIVATE_MARKER)

    monkeypatch.setattr(acceptance, "normalize", broken_normalizer)
    report = acceptance.audit(settings)
    assert report["status"] == "needs_attention"
    assert report["errors"] == ["offline_audit_failed"]
    assert PRIVATE_MARKER not in json.dumps(report)
    assert report["temporary_files_removed"] is True


def test_metadata_difference_is_separate_from_normalization_content(populated_source):
    settings, source = populated_source
    with source._connection:
        source._connection.execute(
            "UPDATE normalized_records SET fetched_at = '2000-01-01T00:00:00Z' "
            "WHERE resource = 'sleep'"
        )
    report = acceptance.audit(settings)
    assert report["status"] == "needs_attention"
    assert report["data_replay"]["normalization_content_mismatches"] == 0
    assert report["data_replay"]["provenance_metadata_mismatches"] == 1


def test_normalizer_version_change_is_informational_if_content_remains_identical(populated_source):
    settings, source = populated_source
    with source._connection:
        source._connection.execute(
            "UPDATE requests SET metrics_json = ? WHERE resource = 'sleep'",
            (json.dumps({"normalization_version": "synthetic-previous-version"}),),
        )
    report = acceptance.audit(settings)
    assert report["status"] == "passed"
    assert report["data_replay"]["normalizer_version_changed_requests"] == 1
    assert report["data_replay"]["all_normalized_records_match"] is True


def test_temporary_directory_errors_are_safe_and_serializable(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError(PRIVATE_MARKER)

    monkeypatch.setattr(acceptance, "TemporaryDirectory", unavailable)
    report = acceptance.audit(Settings(_env_file=None, data_dir=tmp_path))
    assert report["status"] == "needs_attention"
    assert report["errors"] == ["offline_audit_failed"]
    assert PRIVATE_MARKER not in json.dumps(report)
