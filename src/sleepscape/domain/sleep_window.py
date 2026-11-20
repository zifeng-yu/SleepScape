"""Rules for the recorded main sleep window."""

from datetime import UTC, datetime, timedelta

SLEEP_CONFLICTS = frozenset(
    {
        "conflicting_duplicate_record",
        "negative_time_window",
        "sleep_summary_totals_differ",
        "invalid_elapsed_duration",
        "invalid_timestamp",
        "invalid_timestamp_unit_or_range",
    }
)


def utc_instant(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else None
        return result.astimezone(UTC) if result and result.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def sleep_window(sleep, now):
    """Return quality and the measured interval when its bounds are structurally valid."""
    start, end = utc_instant(sleep.get("start_at")), utc_instant(sleep.get("end_at"))
    bounds = None
    if start is None or end is None:
        state = "missing"
    elif start >= end:
        state = "invalid"
    elif end - start > timedelta(hours=24):
        state = "too_long"
    else:
        bounds = (start, end)
        state = "not_closed" if end > now else "available"
    if any(sleep.get(key) is True for key in ("in_progress", "ongoing", "is_sleeping")):
        state = "not_closed"
    quality = sleep.get("quality")
    if sleep.get("core_conflict") is True or (
        isinstance(quality, list)
        and any(code in SLEEP_CONFLICTS for code in quality if isinstance(code, str))
    ):
        state = "conflict"
    return state, bounds
