"""Offline reports from synthetic records, including missingness and stale-source handling."""

from datetime import date, timedelta

import pytest

from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.domain.normalize import normalize
from sleepscape.domain.training_load import load_comparison
from sleepscape.services.verification import public_summary, recorded_loads, verify
from sleepscape.storage import Repository


def baseline_rows(value=10):
    first = date(2026, 1, 1)
    return [{"date": (first + timedelta(days=i)).isoformat(), "load": value} for i in range(30)]


def test_load_comparison_excludes_target_and_future_from_exact_previous_30_days():
    rows = baseline_rows() + [
        {"date": "2026-01-31", "load": 40},
        {"date": "2026-02-01", "load": 999},
    ]
    result = load_comparison(rows, "2026-01-31")
    assert result["baseline_start"] == "2026-01-01"
    assert result["baseline_end"] == "2026-01-30"
    assert result["baseline_valid_days"] == 30
    assert result["baseline_mean"] == 10
    assert result["target_load"] == 40
    assert result["change_percent"] == 300


@pytest.mark.parametrize(
    "missing_mode", ["absent_day", "missing_load", "missing_target", "zero_mean"]
)
def test_missingness_and_zero_denominator_never_produce_a_percentage(missing_mode):
    rows = baseline_rows(value=0 if missing_mode == "zero_mean" else 10)
    if missing_mode == "absent_day":
        rows.pop()
    elif missing_mode == "missing_load":
        rows[0]["load"] = None
    if missing_mode != "missing_target":
        rows.append({"date": "2026-01-31", "load": 40})
    result = load_comparison(rows, "2026-01-31")
    assert result["change_percent"] is None
    if missing_mode in {"absent_day", "missing_load"}:
        assert result["baseline_valid_days"] == 29
        assert result["baseline_mean"] is None
    elif missing_mode == "zero_mean":
        assert result["baseline_valid_days"] == 30
        assert result["baseline_mean"] == 0
    else:
        assert result["target_load"] is None


def activity_record(day, load, key="synthetic-activity"):
    return {
        "resource": "activities",
        "kind": "activity",
        "key": key,
        "date": day,
        "timestamp": None,
        "data": {"training_load": load},
    }


def test_no_recorded_activity_zero_needs_complete_pages_and_current_daily_evidence():
    records = [
        activity_record("2026-01-01", 0),
        activity_record("2026-01-03", None, "missing"),
        activity_record("2026-01-03", 4, "known"),
    ] + [
        {"resource": "daily_summaries", "kind": "daily_summary", "date": f"2026-01-0{i}"}
        for i in range(1, 4)
    ]
    latest = {
        ("activities", f"2026-01-0{i}"): {"status": "ok", "metrics": {"pagination_complete": True}}
        for i in range(1, 5)
    }
    latest.update({("daily_summaries", f"2026-01-0{i}"): {"status": "ok"} for i in range(1, 4)})
    rows = recorded_loads(records, latest, "2026-01-01", "2026-01-04")
    assert [row["load"] for row in rows] == [0, 0, None, None]
    assert [row["status"] for row in rows] == [
        "recorded_activities",
        "no_recorded_activity",
        "partial_activity_load",
        "unknown",
    ]
    assert rows[2]["known_load_sum"] == 4
    latest[("daily_summaries", "2026-01-02")]["status"] = "failed"
    assert recorded_loads(records, latest, "2026-01-02", "2026-01-02")[0]["load"] is None


def plan(first="2026-01-01", last="2026-01-31"):
    return {"history_start": first, "end_date": last, "detail_start": "2026-01-25"}


@pytest.mark.parametrize("failed_status", ["failed", "invalid"])
def test_shifted_failed_scope_marks_retained_rows_stale_and_excludes_them_from_loads(
    tmp_path, failed_status
):
    with Repository(tmp_path / "data") as repo:
        first_run = repo.start_run(plan())
        first_req = DataRequest(
            "activities",
            "2026-01-01:2026-01-31",
            "/synthetic",
            start_date="2026-01-01",
            end_date="2026-01-31",
        )
        rows = [activity_record("2026-01-15", 10)]
        repo.save_snapshot(
            first_run,
            FetchResult(first_req, {"synthetic": "complete"}, "2026-02-01T00:00:00Z", 200),
            NormalizedBatch(rows, metrics={"pagination_complete": True}),
        )
        repo.finish_run(first_run, "completed", {})
        original = repo.list_records()
        second_run = repo.start_run(plan("2026-01-02", "2026-02-01"))
        second_req = DataRequest(
            "activities",
            "2026-01-02:2026-02-01",
            "/synthetic",
            start_date="2026-01-02",
            end_date="2026-02-01",
        )
        if failed_status == "failed":
            repo.record_failure(second_run, second_req, "network_retry_exhausted")
        else:
            repo.save_snapshot(
                second_run,
                FetchResult(second_req, {"synthetic": "invalid"}, "2026-02-02T00:00:00Z", 200),
                NormalizedBatch(metrics={"payload_shape_valid": False}),
            )
        repo.finish_run(second_run, "partial", {})
        report = verify(repo)
        assert repo.list_records() == original
        assert original[0]["request_key"] != second_req.key
        assert report["status"] == "needs_attention"
        assert report["stale_retained_records"] == 1
        day = next(row for row in report["daily_recorded_loads"] if row["date"] == "2026-01-15")
        assert day["load"] is None
        assert day["status"] == "unknown"
        assert report["load_comparison"]["change_percent"] is None


def test_empty_repository_report_and_public_summary_omit_personal_measurements(tmp_path):
    with Repository(tmp_path / "data") as repo:
        assert verify(repo) == {"status": "no_collection_run", "integrity": ["ok"]}
    report = {
        "status": "usable_with_limitations",
        "database_counts": {"normalized_records": 1},
        "load_comparison": {"target_load": "synthetic-sensitive-marker"},
        "daily_recorded_loads": [{"load": "synthetic-sensitive-marker"}],
        "requests": [{"payload": "synthetic-sensitive-marker"}],
    }
    assert public_summary(report) == {
        "status": "usable_with_limitations",
        "database_counts": {"normalized_records": 1},
    }


def test_sleep_day_uses_previous_day_load_and_preceding_30_day_baseline(tmp_path):
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run(
            {"history_start": "2026-01-01", "end_date": "2026-02-01", "detail_start": "2026-02-01"}
        )
        request = DataRequest(
            "activities",
            "2026-01-01:2026-02-01",
            "/synthetic",
            start_date="2026-01-01",
            end_date="2026-02-01",
        )
        rows = [
            activity_record(item["date"], item["load"], key=item["date"])
            for item in baseline_rows()
        ] + [activity_record("2026-01-31", 40, key="target")]
        repo.save_snapshot(
            run_id,
            FetchResult(request, {"synthetic": "complete"}, "2026-02-02T00:00:00Z", 200),
            NormalizedBatch(rows, metrics={"pagination_complete": True}),
        )
        repo.finish_run(run_id, "completed", {})
        comparison = verify(repo)["sleep_day_load_comparisons"][0]
        assert comparison["sleep_date"] == "2026-02-01"
        assert comparison["target_date"] == "2026-01-31"
        assert comparison["target_load"] == 40
        assert comparison["baseline_start"] == "2026-01-01"
        assert comparison["baseline_end"] == "2026-01-30"
        assert comparison["baseline_mean"] == 10
        assert comparison["change_percent"] == 300


def save_synthetic_activities(repo, run_id, first, last, *, omit=None):
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    activities = []
    for offset in range((end - start).days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        if day == omit:
            continue
        activities.append(
            {
                "activityId": f"synthetic-{day}",
                "activityTrainingLoad": 40 if day == "2026-02-07" else 10,
                "startTimeLocal": day + "T12:00:00",
                "startTimeGMT": day + "T04:00:00Z",
                "endTimeGMT": day + "T04:10:00Z",
                "duration": 600,
                "elapsedDuration": 600,
            }
        )
    request = DataRequest(
        "activities", f"{first}:{last}", "/synthetic", start_date=first, end_date=last
    )
    payload = {"pagination_complete": True, "pages": [activities]}
    repo.save_snapshot(
        run_id,
        FetchResult(request, payload, "2026-02-09T00:00:00Z", 200),
        normalize(request, payload, "Asia/Shanghai"),
    )


def rolling_plan():
    return {
        "history_start": "2026-02-01",
        "detail_start": "2026-02-02",
        "end_date": "2026-02-08",
        "complete_day_through": "2026-02-07",
        "mode": "sync",
        "sync_strategy": "rolling",
        "history_days": 7,
    }


def initial_background(repo, omit=None):
    run_id = repo.start_run(
        {
            "history_start": "2026-01-01",
            "detail_start": "2026-02-01",
            "end_date": "2026-02-07",
            "history_days": 38,
        }
    )
    save_synthetic_activities(repo, run_id, "2026-01-01", "2026-02-07", omit=omit)
    repo.finish_run(run_id, "completed", {})


def test_rolling_seven_day_sync_keeps_stored_30_day_baselines_and_original_coverage(tmp_path):
    with Repository(tmp_path / "data") as repo:
        initial_background(repo)
        run_id = repo.start_run(rolling_plan())
        save_synthetic_activities(repo, run_id, "2026-02-01", "2026-02-07")
        repo.finish_run(run_id, "completed", {})
        original = repo.list_records()
        report = verify(repo)
        assert repo.list_records() == original
        assert report["resource_coverage"]["activities"]["record_count"] == 7
        assert report["normalized_counts"]["activity"] == 7
        assert report["load_evaluation_window"] == {
            "start_date": "2026-01-02",
            "end_date": "2026-02-08",
            "complete_day_through": "2026-02-07",
            "source": "stored_records_and_request_status",
        }
        comparisons = report["sleep_day_load_comparisons"]
        assert len(comparisons) == 7
        assert all(item["baseline_valid_days"] == 30 for item in comparisons)
        assert all(item["change_percent"] is not None for item in comparisons)
        assert report["load_comparison"]["target_date"] == "2026-02-07"
        assert report["load_comparison"]["change_percent"] == 300
        assert comparisons[-1]["target_date"] == "2026-02-07"
        assert comparisons[-1]["is_provisional"] is True
        assert comparisons[-1]["sleep_state"] == "pending"
        assert report["daily_recorded_loads"][-1]["date"] == "2026-02-08"
        assert report["daily_recorded_loads"][-1]["status"] == "pending"
        assert report["daily_recorded_loads"][-1]["load"] is None
        readiness = report["display_readiness"]
        assert readiness["available_days"]["full_30_day_baseline_days"] == 7
        assert readiness["complete_day_available_days"]["full_30_day_baseline_days"] == 6
        assert readiness["complete_sleep_days"] == 6
        assert readiness["provisional_sleep_days"] == 1


def test_rolling_sync_does_not_turn_a_real_historical_gap_into_zero(tmp_path):
    with Repository(tmp_path / "data") as repo:
        initial_background(repo, omit="2026-01-15")
        run_id = repo.start_run(rolling_plan())
        save_synthetic_activities(repo, run_id, "2026-02-01", "2026-02-07")
        repo.finish_run(run_id, "completed", {})
        report = verify(repo)
        missing = next(row for row in report["daily_recorded_loads"] if row["date"] == "2026-01-15")
        assert missing["status"] == "unknown"
        assert missing["load"] is None
        assert report["load_comparison"]["baseline_valid_days"] == 29
        assert report["load_comparison"]["change_percent"] is None
        assert report["display_readiness"]["available_days"]["full_30_day_baseline_days"] == 0


def test_historical_failed_attempt_outside_rolling_scope_still_invalidates_baseline(tmp_path):
    with Repository(tmp_path / "data") as repo:
        initial_background(repo)
        failed_run = repo.start_run({**rolling_plan(), "end_date": "2026-02-07"})
        repo.record_failure(
            failed_run,
            DataRequest(
                "activities",
                "recheck-old-day",
                "/synthetic",
                start_date="2026-01-15",
                end_date="2026-01-15",
            ),
            "http_error",
            500,
        )
        repo.finish_run(failed_run, "partial", {})
        run_id = repo.start_run(rolling_plan())
        save_synthetic_activities(repo, run_id, "2026-02-01", "2026-02-07")
        repo.finish_run(run_id, "completed", {})
        report = verify(repo)
        assert report["stale_retained_records"] == 0
        assert report["load_stale_retained_records"] == 1
        assert report["status"] == "needs_attention"
        assert report["load_comparison"]["baseline_valid_days"] == 29


def test_legacy_collect_keeps_end_date_as_last_complete_load_target(tmp_path):
    with Repository(tmp_path / "data") as repo:
        initial_background(repo)
        report = verify(repo)
        assert "complete_day_through" not in report["plan"]
        assert report["load_comparison"]["target_date"] == "2026-02-07"
        assert report["load_comparison"]["baseline_valid_days"] == 30
        assert report["display_readiness"]["provisional_sleep_days"] == 0
        assert all(not item["is_provisional"] for item in report["sleep_day_load_comparisons"])
