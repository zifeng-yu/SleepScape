"""Deterministic statistics for one bounded page of wake dates."""

import math
from datetime import datetime, time
from statistics import median

from sleepscape.domain.morning_feeling import Feeling
from sleepscape.domain.sleep_window import sleep_window

_CARD_METRICS = ("sleep_seconds", "average_heart_rate", "night_average_hrv")


def sleep_offsets(day, sleep, *, now, zone):
    state, bounds = sleep_window(sleep, now)
    if state != "available":
        return None
    start, end = (instant.astimezone(zone) for instant in bounds)
    if end.date() != day:
        return None
    midnight = datetime.combine(day, time.min)
    return (
        (start.replace(tzinfo=None) - midnight).total_seconds(),
        (end.replace(tzinfo=None) - midnight).total_seconds(),
    )


def review_summary(items, offsets, feeling_state):
    values = {key: [] for key in _CARD_METRICS}
    counts = dict.fromkeys((*Feeling, "unfilled"), 0) if feeling_state == "available" else None
    for item in items:
        for key in _CARD_METRICS:
            card = item[key]
            value = card["value"]
            if (
                card["state"] == "available"
                and type(value) in (int, float)
                and math.isfinite(value)
            ):
                values[key].append(value)
        if counts is not None:
            counts[item["morning_feeling"] or "unfilled"] += 1
    starts, ends = zip(*offsets, strict=True) if offsets else ((), ())
    values.update(sleep_start_offset_seconds=starts, sleep_end_offset_seconds=ends)
    return {
        "start_date": items[-1]["date"],
        "end_date": items[0]["date"],
        "night_count": len(items),
        "feeling_state": feeling_state,
        "feeling_counts": counts,
        "medians": {
            key: {"value": median(series) if series else None, "valid_n": len(series)}
            for key, series in values.items()
        },
    }
