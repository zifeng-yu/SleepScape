"""Pure 30-calendar-day comparison; callers decide which observations are usable."""

from datetime import date, timedelta


def load_comparison(daily_loads, target_date: str):
    """Exclude the target day; missing days and a zero mean yield no percentage.

    Verification excludes failed-source values before calling this function.
    The dashboard may keep those same values and label the result stale.
    Neither policy is imposed by this numeric calculation.
    """
    target = date.fromisoformat(target_date)
    lookup = {row["date"]: row for row in daily_loads}
    baseline_dates = [(target - timedelta(days=i)).isoformat() for i in range(30, 0, -1)]
    baseline = [lookup.get(day, {}).get("load") for day in baseline_dates]
    valid = [v for v in baseline if v is not None]
    current = lookup.get(target_date, {}).get("load")
    mean = sum(valid) / 30 if len(valid) == 30 else None
    change = (current / mean - 1) * 100 if mean and current is not None else None
    return {
        "target_date": target_date,
        "target_load": current,
        "baseline_start": baseline_dates[0],
        "baseline_end": baseline_dates[-1],
        "baseline_valid_days": len(valid),
        "baseline_required_days": 30,
        "baseline_mean": mean,
        "change_percent": change,
        "definition": "all recorded activity loads; calendar days; target excluded",
        "zero_definition": (
            "complete API pagination plus daily summary; no recorded activity, not proof of rest"
        ),
    }
