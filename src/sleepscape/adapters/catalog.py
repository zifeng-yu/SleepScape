"""Read-only subset of Garmin's current official Personal API contract."""

import re
from datetime import date, timedelta

from sleepscape.domain.calendar import dates
from sleepscape.domain.models import DataRequest

TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
PATHS = {
    "sleep": "/sleep-service/sleep/dailySleep",
    "sleep_summaries": "/sleep-service/sleep/dailySleepSummariesByDate",
    "heart_rate": "/wellness-service/wellness/dailyHeartRate",
    "hrv": "/hrv-service/hrv/{date}",
    "hrv_summaries": "/hrv-service/hrv/daily/{start}/{end}",
    "daily": "/usersummary-service/usersummary/daily",
    "daily_summaries": "/usersummary-service/v2/dailysummaries",
    "stress": "/usersummary-service/stats/stress/daily/{start}/{end}",
    "steps": "/usersummary-service/stats/steps/daily/{start}/{end}",
    "body_battery": "/wellness-service/wellness/bodyBattery/reports/daily",
    "spo2": "/wellness-service/wellness/daily/spo2acclimation/{date}",
    "wellness_activities": "/wellnessactivity-service/activity/summary/range/{start}/{end}",
    "activities": "/activitylist-service/activities/search/activities",
}
PROVISIONAL_RESOURCES = frozenset(
    {"sleep", "hrv", "heart_rate", "spo2", "body_battery", "wellness_activities"}
)
DATE_PATTERN = r"\d{4}-\d{2}-\d{2}"
ALLOWED_GET_PATHS = tuple(
    re.compile(
        "^"
        + re.escape(path)
        .replace(r"\{date\}", DATE_PATTERN)
        .replace(r"\{start\}", DATE_PATTERN)
        .replace(r"\{end\}", DATE_PATTERN)
        + "$"
    )
    for path in PATHS.values()
)


def daily_request(resource: str, day: date) -> DataRequest:
    value = day.isoformat()
    path = PATHS[resource].format(date=value)
    params: dict[str, str | int] = {}
    if resource in {"sleep", "heart_rate"}:
        params["date"] = value
    elif resource == "daily":
        params["calendarDate"] = value
    return DataRequest(resource, value, path, params, value, value)


def range_request(resource: str, start: date, end: date) -> DataRequest:
    first, last = start.isoformat(), end.isoformat()
    params: dict[str, str | int] = {}
    if resource in {"daily_summaries", "sleep_summaries", "body_battery", "activities"}:
        params.update(startDate=first, endDate=last)
    if resource == "activities":
        params.update(start=0, limit=100, excludeChildren="true")
    return DataRequest(
        resource,
        f"{first}:{last}",
        PATHS[resource].format(start=first, end=last),
        params,
        first,
        last,
    )


def build_plan(end: date, detail_days: int = 7, history_days: int = 31) -> list[DataRequest]:
    if not 1 <= detail_days <= 31 or not detail_days <= history_days <= 366:
        raise ValueError("Require 1 <= detail_days <= 31 and detail_days <= history_days <= 366")
    detail_start = end - timedelta(days=detail_days - 1)
    history_start = end - timedelta(days=history_days - 1)
    requests: list[DataRequest] = []
    for day in dates(detail_start, end):
        for resource in ("sleep", "hrv", "daily", "spo2"):
            requests.append(daily_request(resource, day))
    # A sleep window can start before the calendar date to which Garmin assigns it.
    for day in dates(detail_start - timedelta(days=1), end):
        requests.append(daily_request("heart_rate", day))
    for resource, maximum_days in (
        ("daily_summaries", 365),
        ("sleep_summaries", 31),
        ("hrv_summaries", 28),
        ("stress", 28),
        ("steps", 28),
    ):
        first = history_start
        while first <= end:
            last = min(end, first + timedelta(days=maximum_days - 1))
            requests.append(range_request(resource, first, last))
            first = last + timedelta(days=1)
    for resource in ("body_battery", "wellness_activities"):
        requests.append(range_request(resource, detail_start, end))
    # CN returned HTTP 500 for a 38-day query in validation. Smaller windows
    # keep failures local and bound each pagination transaction; not an API limit.
    first = history_start
    while first <= end:
        last = min(end, first + timedelta(days=6))
        requests.append(range_request("activities", first, last))
        first = last + timedelta(days=1)
    return requests


def build_sync_plan(today: date, detail_days: int = 7, history_days: int = 38) -> list[DataRequest]:
    """Read provisional current-night data, keeping full-day summaries in the past.

    history_days counts completed calendar days ending yesterday. Aggregate sleep
    and HRV history also stops there; today's individual endpoints supply the new
    night. The original build_plan contract remains unchanged.
    """
    yesterday = today - timedelta(days=1)
    historical = build_plan(yesterday, detail_days, history_days)
    detail_start = today - timedelta(days=detail_days - 1)
    requests = []
    for day in dates(detail_start, today):
        for resource in ("sleep", "hrv", "spo2"):
            requests.append(daily_request(resource, day))
    for day in dates(detail_start - timedelta(days=1), today):
        requests.append(daily_request("heart_rate", day))
    for resource in ("body_battery", "wellness_activities"):
        requests.append(range_request(resource, detail_start, today))
    requests.extend(r for r in historical if r.resource not in PROVISIONAL_RESOURCES)
    return requests
