"""Evidence for one rolling seven-day sleep review."""

from datetime import date, timedelta

from sleepscape.domain.dashboard_dates import visible_start


def review_dates(end: date, dashboard_start: date | None) -> list[date]:
    first = visible_start(max(date(2000, 1, 1), end - timedelta(days=6)), dashboard_start)
    return [first + timedelta(days=offset) for offset in range((end - first).days + 1)]


def night_observations(payload):
    """Project only measured values and measurement context for both report scopes."""
    values = {
        fact["metric"]: {"value": fact["value"], "unit": fact["unit"]}
        for fact in payload["facts"]
        if fact["period"] in ("night", "previous_day")
    }
    return {
        "night_date": payload["night_date"],
        "sleep_window_utc": payload["sleep_window"],
        "measurements": values,
        "sound_events": [
            {
                k: event[k]
                for k in ("start_at", "end_at", "active_seconds", "max_1s_rms_dbfs", "merged")
            }
            for event in payload["events"]
        ],
        "sound_event_status": payload["event_status"],
        "quality": payload["completeness"]["quality_state"],
        "limits": payload["required_limitations"],
        "morning_feeling": (payload.get("morning_feeling") or {}).get("value"),
    }
