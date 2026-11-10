"""Synthetic request-plan contract checks; never load account data or use network."""

from collections import defaultdict
from datetime import date, timedelta

import pytest

from sleepscape.adapters.catalog import (
    ALLOWED_GET_PATHS,
    PATHS,
    build_plan,
    daily_request,
    range_request,
)
from sleepscape.domain.calendar import dates

END = date(2025, 1, 5)


def covered_days(requests):
    result = []
    for request in requests:
        result.extend(
            dates(date.fromisoformat(request.start_date), date.fromisoformat(request.end_date))
        )
    return result


@pytest.mark.parametrize(
    "resource,expected_path,expected_params",
    [
        ("sleep", "/sleep-service/sleep/dailySleep", {"date": "2025-01-05"}),
        ("heart_rate", "/wellness-service/wellness/dailyHeartRate", {"date": "2025-01-05"}),
        ("daily", "/usersummary-service/usersummary/daily", {"calendarDate": "2025-01-05"}),
        ("hrv", "/hrv-service/hrv/2025-01-05", {}),
        ("spo2", "/wellness-service/wellness/daily/spo2acclimation/2025-01-05", {}),
    ],
)
def test_daily_official_paths_and_query_parameter_names(resource, expected_path, expected_params):
    request = daily_request(resource, END)
    assert request.path == expected_path
    assert request.params == expected_params
    assert request.start_date == request.end_date == "2025-01-05"


@pytest.mark.parametrize(
    "resource,expected_path",
    [
        ("hrv_summaries", "/hrv-service/hrv/daily/2025-01-01/2025-01-05"),
        ("stress", "/usersummary-service/stats/stress/daily/2025-01-01/2025-01-05"),
        ("steps", "/usersummary-service/stats/steps/daily/2025-01-01/2025-01-05"),
        (
            "wellness_activities",
            "/wellnessactivity-service/activity/summary/range/2025-01-01/2025-01-05",
        ),
    ],
)
def test_range_path_dates_are_inclusive_and_not_duplicated_as_query(resource, expected_path):
    request = range_request(resource, date(2025, 1, 1), END)
    assert request.path == expected_path
    assert request.params == {}
    assert covered_days([request]) == dates(date(2025, 1, 1), END)


@pytest.mark.parametrize(
    "resource,expected_path",
    [
        ("sleep_summaries", "/sleep-service/sleep/dailySleepSummariesByDate"),
        ("daily_summaries", "/usersummary-service/v2/dailysummaries"),
        ("body_battery", "/wellness-service/wellness/bodyBattery/reports/daily"),
    ],
)
def test_range_query_uses_official_start_and_end_names(resource, expected_path):
    request = range_request(resource, date(2025, 1, 1), END)
    assert request.path == expected_path
    assert request.params == {"startDate": "2025-01-01", "endDate": "2025-01-05"}


def test_all_sports_query_avoids_parent_child_double_counting():
    request = range_request("activities", date(2025, 1, 1), END)
    assert request.path == "/activitylist-service/activities/search/activities"
    assert request.params == {
        "startDate": "2025-01-01",
        "endDate": "2025-01-05",
        "start": 0,
        "limit": 100,
        "excludeChildren": "true",
    }
    assert not {"activityType", "activitySubType"}.intersection(request.params)


def test_inclusive_dates_cover_leap_day_and_year_boundary():
    assert dates(date(2024, 2, 28), date(2024, 3, 1)) == [
        date(2024, 2, 28),
        date(2024, 2, 29),
        date(2024, 3, 1),
    ]
    assert dates(date(2024, 12, 31), date(2025, 1, 1)) == [date(2024, 12, 31), date(2025, 1, 1)]
    assert dates(END, END) == [END]
    assert dates(END, END - timedelta(days=1)) == []


@pytest.mark.parametrize("history_days", [28, 29, 31, 32, 38, 365, 366])
def test_chunk_boundaries_cover_full_history_without_gaps_or_duplicate_dates(history_days):
    plan = build_plan(END, detail_days=1, history_days=history_days)
    first = END - timedelta(days=history_days - 1)
    expected = dates(first, END)
    for resource, maximum in {
        "daily_summaries": 365,
        "sleep_summaries": 31,
        "hrv_summaries": 28,
        "stress": 28,
        "steps": 28,
        "activities": 7,
    }.items():
        requests = [request for request in plan if request.resource == resource]
        assert covered_days(requests) == expected
        assert all(1 <= len(covered_days([request])) <= maximum for request in requests)
        assert len(requests) <= (history_days + maximum - 1) // maximum


def test_default_details_have_one_extra_heart_rate_day_for_cross_midnight_sleep():
    groups = defaultdict(list)
    for request in build_plan(END):
        groups[request.resource].append(request)
    detail_start = END - timedelta(days=6)
    for resource in ("sleep", "hrv", "daily", "spo2"):
        assert covered_days(groups[resource]) == dates(detail_start, END)
    assert covered_days(groups["heart_rate"]) == dates(detail_start - timedelta(days=1), END)
    assert covered_days(groups["activities"]) == dates(END - timedelta(days=30), END)


def test_explicit_38_days_cover_previous_day_and_30_prior_days_for_all_seven_sleeps():
    plan = build_plan(END, detail_days=7, history_days=38)
    activity_days = set(covered_days([r for r in plan if r.resource == "activities"]))
    summary_days = set(covered_days([r for r in plan if r.resource == "daily_summaries"]))
    for sleep_day in dates(END - timedelta(days=6), END):
        target = sleep_day - timedelta(days=1)
        required = set(dates(target - timedelta(days=30), target))
        assert required <= activity_days
        assert required <= summary_days
    assert min(activity_days) == END - timedelta(days=37)
    assert max(activity_days) == END


def test_31_days_only_guarantee_latest_activity_day_comparison_not_seven_sleep_days():
    activity_days = set(covered_days([r for r in build_plan(END) if r.resource == "activities"]))
    assert set(dates(END - timedelta(days=30), END)) <= activity_days
    sleep_target = END - timedelta(days=1)
    assert sleep_target - timedelta(days=30) not in activity_days


@pytest.mark.parametrize("detail,history", [(0, 31), (-1, 31), (32, 38), (7, 6), (1, 0), (1, 367)])
def test_invalid_plan_bounds_fail_before_any_request_is_generated(detail, history):
    with pytest.raises(ValueError):
        build_plan(END, detail_days=detail, history_days=history)


def test_maximum_plan_is_finite_unique_and_inside_read_scope():
    plan = build_plan(END, detail_days=31, history_days=366)
    assert 0 < len(plan) < 300
    assert len({(request.resource, request.key) for request in plan}) == len(plan)
    assert {request.resource for request in plan} == {
        "sleep",
        "sleep_summaries",
        "heart_rate",
        "hrv",
        "hrv_summaries",
        "daily",
        "daily_summaries",
        "stress",
        "steps",
        "body_battery",
        "spo2",
        "wellness_activities",
        "activities",
    }
    for request in plan:
        assert request.resource in PATHS
        assert any(pattern.fullmatch(request.path) for pattern in ALLOWED_GET_PATHS)
        assert "{" not in request.path and "?" not in request.path
        assert date.fromisoformat(request.start_date) <= date.fromisoformat(request.end_date) <= END


@pytest.mark.parametrize(
    "path",
    [
        "/userprofile-service/userprofile/settings",
        "/download-service/files/activity/123",
        "/sleep-service/sleep/dailySleep/extra",
        "/sleep-service/sleep/dailySleep?date=2025-01-05",
        "https://example.invalid/sleep-service/sleep/dailySleep",
    ],
)
def test_allowlist_excludes_unrequested_data_and_embedded_url_or_query(path):
    assert not any(pattern.fullmatch(path) for pattern in ALLOWED_GET_PATHS)
