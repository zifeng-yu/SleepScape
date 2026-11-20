"""Synthetic acceptance of dashboard semantics and browser field allowlists."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sleepscape.config import Settings
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.domain.morning_feeling import Feeling
from sleepscape.domain.sleep_review import sleep_offsets
from sleepscape.services.views import DashboardService
from sleepscape.storage import morning_feeling
from sleepscape.storage.repository import Repository

DAY = date(2026, 2, 1)
AT = "2026-02-02T01:00:00Z"
PRIVATE = "SYNTHETIC_PRIVATE_VALUE_DO_NOT_EXPOSE"


def row(kind, day, data, key=None, timestamp=None):
    return {
        "kind": kind,
        "key": key or day,
        "date": day,
        "timestamp": timestamp,
        "data": {
            **data,
            "raw": {"owner": PRIVATE, "GPS": PRIVATE},
            "activity_id": PRIVATE,
            "owner": PRIVATE,
        },
    }


def save(repository, run, resource, records, start, end=None, metrics=None):
    request = DataRequest(
        resource, f"{start}:{end or start}", "/synthetic", start_date=start, end_date=end or start
    )
    repository.save_snapshot(
        run,
        FetchResult(request, {"synthetic": records}, AT, 200),
        NormalizedBatch(records, metrics=metrics or {}),
    )
    return request


def sleep_row(day="2026-02-01", **extra):
    return row(
        "sleep",
        day,
        {
            "start_at": "2026-01-31T15:00:00Z",
            "end_at": "2026-01-31T23:00:00Z",
            "sleep_seconds": 28000,
            "average_heart_rate": 55,
            "average_respiration": 14,
            "sleep_score": 80,
            "deep_seconds": 5000,
            "light_seconds": 18000,
            "rem_seconds": 5000,
            "awake_seconds": 800,
            "unmeasurable_seconds": None,
            "stage_timeline_available": False,
            **extra,
        },
    )


@pytest.fixture
def service(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", timezone="Asia/Shanghai")
    result = DashboardService(settings)
    result._now = lambda: datetime(2026, 2, 2, 2, tzinfo=UTC)
    return result


def seed(service, *, load=100, target=300):
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({"end_date": "2026-02-01", "detail_start": "2026-01-26"})
        activities, dailies = [], []
        for i in range(31):
            day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
            activities.append(
                row(
                    "activity",
                    day,
                    {
                        "training_load": load if i < 30 else target,
                        "sport_type": "running",
                        "duration_seconds": 100,
                        "start_at": day + "T08:00:00Z",
                        "end_at": day + "T08:02:00Z",
                    },
                )
            )
            dailies.append(row("daily_summary", day, {"steps": 5000, "average_stress": 25}))
        save(
            repository,
            run,
            "activities",
            activities,
            "2026-01-01",
            "2026-01-31",
            {"pagination_complete": True},
        )
        save(repository, run, "daily_summaries", dailies, "2026-01-01", "2026-01-31")
        save(repository, run, "sleep", [sleep_row()], "2026-02-01")
        save(
            repository,
            run,
            "hrv",
            [row("hrv_daily", "2026-02-01", {"night_average_ms": 40, "weekly_average_ms": 42})],
            "2026-02-01",
        )
        repository.finish_run(run, "completed", {"stop_reason": None})
    return run


def test_previous_all_sport_load_excludes_sleep_day_and_reads_history_beyond_latest_run(service):
    seed(service)
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({"end_date": "2026-02-01", "history_start": "2026-01-26"})
        save(
            repository,
            run,
            "activities",
            [
                row(
                    "activity", "2026-01-31", {"training_load": 100, "sport_type": "running"}, "run"
                ),
                row(
                    "activity",
                    "2026-01-31",
                    {"training_load": 200, "sport_type": "cycling"},
                    "bike",
                ),
                row("activity", "2026-02-01", {"training_load": 9000}, "after_wake"),
            ],
            "2026-01-31",
            "2026-02-01",
            {"pagination_complete": True},
        )
        repository.finish_run(run, "completed", {})
    night = service.night(DAY)
    comparison = night["load_comparison"]
    assert night["cards"]["training_load"]["value"] == 300
    assert comparison["target_date"] == "2026-01-31"
    assert comparison["baseline_start"] == "2026-01-01"
    assert comparison["baseline_end"] == "2026-01-30"
    assert comparison["baseline_valid_days"] == comparison["baseline_fresh_days"] == 30
    assert comparison["baseline_mean"] == 100
    assert comparison["change_percent"] == 200
    assert comparison["state"] == "available"
    assert night["cards"]["previous_steps"]["source_date"] == "2026-01-31"


def test_failures_keep_old_values_but_stale_baseline_is_never_fresh(service):
    seed(service)
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        repository.record_failure(
            run,
            DataRequest(
                "activities",
                "different_window",
                "/synthetic",
                start_date="2026-01-10",
                end_date="2026-01-15",
            ),
            "http_error",
            500,
        )
        repository.record_failure(
            run,
            DataRequest(
                "sleep", "2026-02-01", "/synthetic", start_date="2026-02-01", end_date="2026-02-01"
            ),
            "http_error",
            500,
        )
        repository.finish_run(run, "partial", {})
    night = service.night(DAY)
    assert night["cards"]["average_heart_rate"]["value"] == 55
    assert night["cards"]["average_heart_rate"]["state"] == "stale"
    comparison = night["load_comparison"]
    assert comparison["baseline_mean"] == 100
    assert comparison["baseline_valid_days"] == 30
    assert comparison["baseline_fresh_days"] == 24
    assert comparison["state"] == "stale"
    assert "latest_request_failed" in night["quality"]


def test_missing_and_unknown_loads_are_not_zero_and_zero_baseline_has_no_percent(service):
    seed(service, load=0, target=1)
    comparison = service.night(DAY)["load_comparison"]
    assert comparison["baseline_mean"] == 0
    assert comparison["change_percent"] is None
    assert comparison["reason"] == "baseline_zero"
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(
            repository,
            run,
            "activities",
            [row("activity", "2026-01-15", {"training_load": None})],
            "2026-01-15",
            metrics={"pagination_complete": True},
        )
    comparison = service.night(DAY)["load_comparison"]
    assert comparison["baseline_valid_days"] == 29
    assert comparison["baseline_mean"] is None
    assert comparison["change_percent"] is None
    assert comparison["reason"] == "baseline_incomplete"


def test_complete_empty_activity_day_requires_daily_summary_for_zero(service):
    seed(service)
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(repository, run, "activities", [], "2026-01-31", metrics={"pagination_complete": True})
    load = service.night(DAY)["cards"]["training_load"]
    assert load["value"] == 0 and load["reason"] == "no_recorded_activity"
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(repository, run, "daily_summaries", [], "2026-01-31")
    load = service.night(DAY)["cards"]["training_load"]
    assert load["value"] is None and load["reason"] == "daily_summary_missing"


def test_sleep_source_precedence_fallback_and_missing_value_survive_current_day(service):
    seed(service)
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(repository, run, "sleep_summaries", [sleep_row(average_heart_rate=70)], "2026-02-01")
    assert service.night(DAY)["cards"]["average_heart_rate"]["value"] == 55
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(repository, run, "sleep", [], "2026-02-01")
    night = service.night(DAY)
    assert night["cards"]["average_heart_rate"]["value"] == 70
    assert night["cards"]["average_heart_rate"]["source_resource"] == "sleep_summaries"
    assert "sleep_summary_fallback" in night["quality"]
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(
            repository,
            run,
            "sleep_summaries",
            [sleep_row(average_heart_rate=None, quality={"average_heart_rate": "null"})],
            "2026-02-01",
        )
    assert service.night(DAY)["cards"]["average_heart_rate"]["state"] == "missing"
    service._now = lambda: datetime(2026, 2, 1, 2, tzinfo=UTC)
    night = service.night(DAY)
    assert night["is_provisional"] is False
    assert night["cards"]["sleep_seconds"]["state"] == "available"
    assert night["cards"]["average_heart_rate"]["state"] == "missing"
    assert "current_day_provisional" not in night["quality"]


def test_observed_cards_and_history_do_not_change_state_at_midnight(service):
    seed(service)
    service._now = lambda: datetime(2026, 2, 1, 15, 59, tzinfo=UTC)
    before = service.night(DAY)
    history_before = service.nights(DAY, DAY)["items"]
    assert all(
        before["cards"][key]["state"] == "available"
        for key in ("sleep_seconds", "average_heart_rate", "night_average_hrv", "training_load")
    )
    assert not before["is_provisional"]
    assert "current_day_provisional" not in before["quality"]
    service._now = lambda: datetime(2026, 2, 1, 16, 1, tzinfo=UTC)
    after = service.night(DAY)
    assert before["cards"] == after["cards"]
    assert before["stages"] == after["stages"]
    assert before["quality"] == after["quality"]
    assert history_before == service.nights(DAY, DAY)["items"]


def test_current_day_invalid_and_failed_source_states_remain_explicit(service):
    seed(service)
    service._now = lambda: datetime(2026, 2, 1, 2, tzinfo=UTC)
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(
            repository,
            run,
            "sleep",
            [sleep_row(average_heart_rate=None, quality={"average_heart_rate": "invalid_number"})],
            DAY.isoformat(),
        )
        repository.record_failure(
            run,
            DataRequest(
                "hrv", "latest-failed", "/synthetic", start_date=str(DAY), end_date=str(DAY)
            ),
            "http_error",
            500,
        )
        repository.finish_run(run, "partial", {})
    cards = service.night(DAY)["cards"]
    assert cards["sleep_seconds"]["state"] == "available"
    assert cards["average_heart_rate"]["state"] == "invalid"
    assert cards["average_heart_rate"]["value"] is None
    assert cards["night_average_hrv"]["state"] == "stale"
    assert cards["night_average_hrv"]["value"] == 40
    assert cards["night_average_hrv"]["reason"] == "latest_request_failed"


def test_current_night_timeline_distinguishes_observed_and_absent_samples(service):
    seed(service)
    service._now = lambda: datetime(2026, 2, 1, 2, tzinfo=UTC)
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(
            repository,
            run,
            "heart_rate",
            [row("heart_rate_sample", str(DAY), {"bpm": 62}, "measured", "2026-01-31T16:01:00Z")],
            str(DAY),
        )
        repository.finish_run(run, "completed", {})
    series = {item["metric"]: item for item in service.timeline(DAY)["series"]}
    assert series["heart_rate"]["state"] == "available"
    assert series["heart_rate"]["points"][0]["value"] == 62
    for key in ("hrv", "spo2", "body_battery"):
        assert series[key]["state"] == "missing"
        assert series[key]["points"] == []


@pytest.mark.parametrize("pagination_complete", [False, True])
def test_unfinished_activity_day_never_becomes_a_complete_load_or_zero(
    service, pagination_complete
):
    seed(service)
    service._now = lambda: datetime(2026, 2, 1, 2, tzinfo=UTC)
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(
            repository,
            run,
            "activities",
            [row("activity", str(DAY), {"training_load": 25}, "current-activity")],
            str(DAY),
            metrics={"pagination_complete": pagination_complete},
        )
        repository.finish_run(run, "completed", {})
    # A future sleep-date history row would otherwise use today's partial activity day.
    future = service.nights(DAY + timedelta(days=1), DAY + timedelta(days=1))["items"][0]
    load = future["training_load"]
    assert load["value"] is None
    assert load["state"] == "missing"
    assert load["reason"] == (
        "activity_day_not_finished" if pagination_complete else "activity_scope_incomplete"
    )
    assert load["known_load_sum"] == 25 and load["activity_count"] == 1
    assert future["change_percent"]["value"] is None
    assert future["change_percent"]["state"] == "missing"


def test_timeline_joins_actual_utc_across_source_dates_and_keeps_hourly_separate(service):
    seed(service)
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(
            repository,
            run,
            "hrv",
            [
                row("hrv_daily", "2026-02-01", {"night_average_ms": 40}),
                row("hrv_sample", "2026-02-01", {"hrv_ms": 32}, "sample", "2026-01-31T16:00:00Z"),
            ],
            "2026-02-01",
        )
        save(
            repository,
            run,
            "heart_rate",
            [
                row(
                    "heart_rate_sample",
                    "2026-01-31",
                    {"bpm": 60, "series_type": "native"},
                    "before_midnight",
                    "2026-01-31T15:59:00Z",
                ),
                row(
                    "heart_rate_sample",
                    "2026-02-01",
                    {"bpm": 62, "series_type": "native"},
                    "after_midnight",
                    "2026-01-31T16:01:00Z",
                ),
            ],
            "2026-01-31",
            "2026-02-01",
        )
        save(
            repository,
            run,
            "spo2",
            [
                row(
                    "spo2_sample",
                    "2026-02-01",
                    {"percent": 97, "series_type": "native"},
                    "native",
                    "2026-01-31T16:00:00Z",
                ),
                row(
                    "spo2_sample",
                    "2026-02-01",
                    {"percent": 1, "series_type": "native", "source_plottable": False},
                    "not_plottable",
                    "2026-01-31T16:01:00Z",
                ),
                row(
                    "spo2_sample",
                    "2026-02-01",
                    {"percent": 96, "series_type": "hourly_average"},
                    "hourly",
                    "2026-01-31T16:00:00Z",
                ),
            ],
            "2026-02-01",
        )
        save(
            repository,
            run,
            "activities",
            [
                row(
                    "activity",
                    "2026-01-30",
                    {
                        "start_at": "2026-01-31T16:00:00Z",
                        "end_at": "2026-01-31T16:05:00Z",
                        "training_load": 2,
                        "sport_type": "running",
                        "quality": {"date": "source_date_differs_from_configured_date"},
                    },
                    "travel",
                )
            ],
            "2026-01-30",
            metrics={"pagination_complete": True},
        )
    timeline = service.timeline(DAY)
    series = {item["id"]: item for item in timeline["series"]}
    assert len(series["heart_rate:native"]["points"]) == 2
    assert series["hrv:native"]["points"][0]["source_date"] == "2026-02-01"
    assert series["spo2:native"]["display_resolution"]["original_count"] == 2
    assert series["spo2:native"]["display_resolution"]["valid_count"] == 1
    assert series["spo2:hourly_average"]["aggregation_window_seconds"] == 3600
    assert series["spo2:hourly_average"]["points"][0]["value"] == 96
    assert timeline["stages"] == []
    travel = next(event for event in timeline["events"] if event["source_date"] == "2026-01-30")
    assert "source_date_differs_from_configured_date" in travel["quality"]
    assert PRIVATE not in json.dumps(timeline)


def test_large_timeline_returns_at_most_requested_original_observations(service):
    seed(service)
    first = datetime(2026, 1, 31, 15, tzinfo=UTC)
    rows = [
        row(
            "heart_rate_sample",
            "2026-01-31",
            {"bpm": 50 + i % 60},
            str(i),
            (first + timedelta(seconds=i)).isoformat(),
        )
        for i in range(3000)
    ]
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        save(repository, run, "heart_rate", rows, "2026-01-31")
    series = next(
        item
        for item in service.timeline(DAY, max_points=20)["series"]
        if item["metric"] == "heart_rate"
    )
    assert series["display_resolution"]["original_count"] == 3000
    assert series["display_resolution"]["method"] == "minmax"
    assert len(series["points"]) <= 20
    assert all(
        point["timestamp"] in {item["timestamp"] for item in rows} for point in series["points"]
    )


def test_history_pagination_keeps_missing_dates_and_public_data_has_no_private_fields(service):
    seed(service)
    page = service.nights(DAY - timedelta(days=1), DAY + timedelta(days=1), limit=2)
    assert [item["date"] for item in page["items"]] == ["2026-02-02", "2026-02-01"]
    assert page["next_cursor"] == "2026-01-31"
    assert page["available_dates"] == ["2026-02-01"]
    assert page["items"][0]["sleep_seconds"]["value"] is None
    assert (
        len(
            service.nights(
                DAY - timedelta(days=1),
                DAY + timedelta(days=1),
                cursor=date.fromisoformat(page["next_cursor"]),
            )["items"]
        )
        == 1
    )
    for response in (page, service.night(DAY), service.timeline(DAY), service.status()):
        text = json.dumps(response, allow_nan=False)
        assert PRIVATE not in text
        assert str(service.settings.data_dir) not in text
        assert '"raw"' not in text and '"activity_id"' not in text
        assert '"request_key"' not in text and '"snapshot_id"' not in text


def test_review_uses_visible_page_latest_feeling_and_independent_metric_counts(service):
    first, second, rated_only = date(2026, 1, 31), DAY, date(2026, 2, 2)
    service.settings.dashboard_start_date = first
    service._now = lambda: datetime(2026, 2, 3, 2, tzinfo=UTC)
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({"end_date": rated_only.isoformat()})
        save(
            repository,
            run,
            "sleep_summaries",
            [
                sleep_row(
                    first.isoformat(),
                    start_at="2026-01-30T15:50:00Z",
                    end_at="2026-01-30T23:10:00Z",
                    sleep_seconds=25000,
                    average_heart_rate=None,
                    quality={"average_heart_rate": "invalid_number"},
                ),
                sleep_row(second.isoformat(), sleep_seconds=1),
            ],
            first.isoformat(),
            second.isoformat(),
        )
        save(
            repository,
            run,
            "sleep",
            [
                sleep_row(
                    start_at="2026-01-31T16:10:00Z",
                    end_at="2026-01-31T23:30:00Z",
                    sleep_seconds=26000,
                    average_heart_rate=60,
                )
            ],
            second.isoformat(),
        )
        save(
            repository,
            run,
            "hrv",
            [row("hrv_daily", first.isoformat(), {"night_average_ms": 54})],
            first.isoformat(),
        )
        repository.finish_run(run, "completed", {})
    directory = service.settings.data_dir
    morning_feeling.migrate(directory)
    morning_feeling.save(directory, first - timedelta(days=1), Feeling.POOR)
    morning_feeling.save(directory, first, Feeling.FAIR)
    morning_feeling.save(directory, rated_only, Feeling.GOOD)
    morning_feeling.save(directory, rated_only, Feeling.POOR)

    page = service.nights(first - timedelta(days=5), rated_only, include_review=True)
    assert [item["morning_feeling"] for item in page["items"]] == ["poor", None, "fair"]
    assert page["available_dates"] == [second.isoformat(), first.isoformat()]
    assert page["items"][0]["sleep_seconds"]["state"] == "missing"
    assert page["review"] == {
        "start_date": first.isoformat(),
        "end_date": rated_only.isoformat(),
        "night_count": 3,
        "feeling_state": "available",
        "feeling_counts": {"poor": 1, "fair": 1, "good": 0, "unfilled": 1},
        "medians": {
            "sleep_seconds": {"value": 25500, "valid_n": 2},
            "average_heart_rate": {"value": 60, "valid_n": 1},
            "night_average_hrv": {"value": 54, "valid_n": 1},
            "sleep_start_offset_seconds": {"value": 0, "valid_n": 2},
            "sleep_end_offset_seconds": {"value": 26400, "valid_n": 2},
        },
    }
    assert "filled_at" not in json.dumps(page) and PRIVATE not in json.dumps(page)
    first_page = service.nights(first, rated_only, limit=2, include_review=True)
    assert first_page["next_cursor"] == first.isoformat()
    assert first_page["review"]["start_date"] == second.isoformat()
    assert first_page["review"]["night_count"] == 2
    next_page = service.nights(first, rated_only, cursor=first, limit=2, include_review=True)
    assert next_page["review"]["end_date"] == first.isoformat()
    assert next_page["review"]["feeling_counts"]["fair"] == 1
    hidden_day = first - timedelta(days=1)
    assert service.nights(hidden_day, hidden_day, include_review=True)["review"] is None
    default_page = service.nights(first, rated_only)
    assert "review" not in default_page
    assert all("morning_feeling" not in item for item in default_page["items"])


def test_review_degrades_rating_reads_and_excludes_stale_source(service):
    empty = service.nights(DAY, DAY, include_review=True)["review"]
    assert empty["feeling_state"] == "not_prepared" and empty["feeling_counts"] is None
    assert not service.settings.data_dir.exists()
    seed(service)
    old_database = service.settings.data_dir / "sleepscape.sqlite3"
    old_bytes = old_database.read_bytes()
    old_files = set(old_database.parent.iterdir())
    snapshot = service.nights(DAY, DAY, include_review=True)["review"]
    assert snapshot["feeling_state"] == "not_prepared"
    assert snapshot["medians"]["sleep_seconds"] == {"value": 28000, "valid_n": 1}
    assert old_database.read_bytes() == old_bytes
    # SQLite may open journal coordination files even for a read-only WAL reader.
    wal = old_database.with_name(old_database.name + "-wal")
    shm = old_database.with_name(old_database.name + "-shm")
    assert set(old_database.parent.iterdir()) - {wal, shm} == old_files - {wal, shm}
    assert not wal.exists() or wal.stat().st_size == 0

    with closing(sqlite3.connect(old_database)) as connection, connection:
        connection.execute("CREATE TABLE morning_feeling (unexpected TEXT)")
    with Repository(service.settings.data_dir) as repository:
        run = repository.start_run({})
        request = DataRequest(
            "sleep",
            f"{DAY}:{DAY}",
            "/synthetic",
            start_date=DAY.isoformat(),
            end_date=DAY.isoformat(),
        )
        repository.record_failure(run, request, "network_retry_exhausted")
        repository.finish_run(run, "partial", {})
    page = service.nights(DAY, DAY, include_review=True)
    assert page["items"][0]["sleep_seconds"]["state"] == "stale"
    assert page["items"][0]["sleep_seconds"]["value"] == 28000
    assert page["review"]["feeling_state"] == "unavailable"
    assert page["review"]["feeling_counts"] is None
    assert page["review"]["medians"]["sleep_seconds"] == {"value": None, "valid_n": 0}
    assert page["review"]["medians"]["sleep_start_offset_seconds"] == {"value": None, "valid_n": 0}
    assert page["review"]["medians"]["night_average_hrv"] == {"value": 40, "valid_n": 1}


@pytest.mark.parametrize(
    "changes",
    [
        {"start_at": "2026-02-01T11:00:00"},
        {"start_at": "2026-02-01T15:00:00Z"},
        {"end_at": "2026-02-01T05:00:00Z"},
        {"end_at": "2026-02-01T07:00:00Z"},
        {"start_at": "2026-01-31T12:00:00Z"},
        {"end_at": "2026-02-02T00:00:00Z"},
        {"in_progress": True},
        {"quality": ["conflicting_duplicate_record"]},
    ],
)
def test_review_uses_configured_local_clock_and_only_valid_windows(changes):
    zone = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 2, 1, 21, tzinfo=UTC)
    day = date(2026, 2, 1)
    sleep = {"start_at": "2026-02-01T06:30:00Z", "end_at": "2026-02-01T14:00:00Z"}
    assert sleep_offsets(day, sleep, now=now, zone=zone) == (-5400, 21600)
    assert sleep_offsets(day, {**sleep, **changes}, now=now, zone=zone) is None


def test_status_reports_36h_freshness_and_only_safe_collection_fields(service):
    seed(service)
    status = service.status()
    assert status["stale"] is False
    assert status["latest_night"] == "2026-02-01"
    assert status["latest_collection"]["status"] == "completed"
    assert status["backup"]["state"] == "not_configured"
    service._now = lambda: datetime(2026, 2, 4, 2, tzinfo=UTC)
    assert service.status()["stale"] is True


def test_missing_database_bounds_and_empty_state(service):
    assert service.night(DAY) is None
    assert service.timeline(DAY) is None
    assert service.status()["database"]["present"] is False
    assert service.nights(DAY, DAY)["items"][0]["sleep_seconds"]["state"] == "missing"
    assert not service.settings.data_dir.exists()
    with pytest.raises(ValueError):
        service.nights(DAY, DAY + timedelta(days=92))
    with pytest.raises(ValueError):
        service.nights(DAY, DAY, cursor=DAY - timedelta(days=1))
    with pytest.raises(ValueError):
        service.timeline(DAY, max_points=2001)


def test_backup_status_accepts_only_bounded_verified_metadata(service):
    path = service.settings.data_dir / "reports" / "backup-status.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"status": "verified", "last_success_at": AT, "record_count": 3, "private": PRIVATE}
        )
    )
    assert service.status()["backup"] == {"state": "verified", "last_success_at": AT}
    assert PRIVATE not in json.dumps(service.status())
    path.write_text("{")
    assert service.status()["backup"]["state"] == "unknown"
    path.write_text(json.dumps({"status": "verified", "last_success_at": "not a timestamp"}))
    assert service.status()["backup"]["state"] == "unknown"
    path.write_text(" " * 4097)
    assert service.status()["backup"]["state"] == "unknown"
    path.unlink()
    assert service.status()["backup"]["state"] == "not_configured"
    private = path.parent / "private.json"
    private.write_text(json.dumps({"status": "verified", "last_success_at": AT}))
    path.symlink_to(private)
    assert service.status()["backup"]["state"] == "unknown"
