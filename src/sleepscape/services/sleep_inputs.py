"""A coherent, read-only snapshot for private evidence and report comparisons."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from sleepscape.domain.analysis_readiness import evaluate_readiness
from sleepscape.domain.daily import instant
from sleepscape.domain.sleep_features import build_sleep_input
from sleepscape.resources import validate_sleep_input
from sleepscape.services.readiness import _environment, _fingerprint, _semantic
from sleepscape.services.views import DashboardService
from sleepscape.storage.morning_feeling import read_range
from sleepscape.storage.reader import DashboardReader


@contextmanager
def _snapshot_reader(settings, connection):
    previous_factory = connection.row_factory if connection is not None else None
    if connection is not None:
        connection.row_factory = sqlite3.Row
    try:
        with DashboardReader(settings.data_dir, connection=connection) as reader:
            yield reader
    finally:
        if connection is not None:
            connection.row_factory = previous_factory


def read_snapshot(
    settings, day: date, *, now=None, build=True, connection=None, include_morning_feeling=True
):
    """Release the SQLite transaction before returning; never call a model or write."""
    if type(day) is not date or not 2000 <= day.year <= 2100:
        raise ValueError("invalid_analysis_date")
    clock = instant(now if now is not None else datetime.now(UTC))
    view = DashboardService(settings)
    view._now = lambda: clock
    with _snapshot_reader(settings, connection) as reader:
        night = view.readiness_night(reader, day)
        environment = _environment(settings, reader, night, clock)
        readiness = evaluate_readiness(
            day, night, environment, now=clock, lookback_days=settings.garmin_lookback_days
        )
        readiness["source_fingerprint"] = _fingerprint(night, environment, readiness)
        baselines = [
            view.readiness_night(reader, day - timedelta(days=offset))
            for offset in range(30, 0, -1)
        ]
        morning_feeling = None
        feeling_state = None
        if include_morning_feeling:
            feeling_state, feelings = read_range(reader.connection, day, day)
            morning_feeling = feelings.get(day.isoformat())
    # Baselines are source data too: a later historical correction must be noticed.
    baseline_identity = [
        {
            "date": item.get("date", item.get("night_date")),
            "sleep": item.get("sleep"),
            "cards": {
                key: value
                for key, value in item.get("cards", {}).items()
                if key in {"sleep_seconds", "average_heart_rate", "night_average_hrv"}
            },
        }
        for item in baselines
    ]
    source = {"night": readiness["source_fingerprint"], "baselines": _semantic(baseline_identity)}
    if morning_feeling is not None:
        source["morning_feeling"] = morning_feeling.value
    fingerprint = hashlib.sha256(
        json.dumps(
            source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    payload = None
    if build and readiness["data_state"] == "ready":
        payload = build_sleep_input(
            day,
            night,
            environment,
            readiness,
            baseline_nights=baselines,
            data_cutoff_at=clock,
            morning_feeling=morning_feeling,
        )
        validate_sleep_input(payload)
    return {
        "readiness": readiness,
        "input": payload,
        "source_fingerprint": fingerprint,
        "feeling_state": feeling_state,
        "time_mapping_version": (environment or {}).get("time_mapping_algorithm") or "unknown",
    }
