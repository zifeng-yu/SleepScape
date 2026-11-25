"""Evidence for one rolling seven-day sleep review."""

from collections import Counter
from datetime import date, timedelta
from itertools import combinations
from statistics import median

from sleepscape.domain.dashboard_dates import visible_start
from sleepscape.domain.sleep_features import environment_minutes, environment_statistics
from sleepscape.domain.sleep_review import sleep_offsets
from sleepscape.domain.sleep_window import utc_instant

METRIC_UNITS = {
    "sleep_seconds": "s",
    "sleep_start_offset_seconds": "s",
    "sleep_end_offset_seconds": "s",
    "average_heart_rate": "bpm",
    "night_average_hrv": "ms",
    "training_load": "load",
    "temperature_c": "degC",
    "relative_humidity_pct": "percent",
    "illuminance_lux": "lx",
    "sound_relative": "dBFS",
}
_ENVIRONMENT = frozenset(tuple(METRIC_UNITS)[6:])
_CARDS = ("sleep_seconds", "average_heart_rate", "night_average_hrv", "training_load")
_FEELINGS = ("poor", "fair", "good", "unfilled")


def review_dates(end: date, dashboard_start: date | None) -> list[date]:
    first = visible_start(max(date(2000, 1, 1), end - timedelta(days=6)), dashboard_start)
    return [first + timedelta(days=offset) for offset in range((end - first).days + 1)]


def nightly_evidence(day, night, environment, readiness, *, now, zone):
    """Extract measurements using the same accepted minutes and weights as P4."""
    start, end = (utc_instant(readiness["window"][key]) for key in ("start", "end"))
    rows = environment_minutes(environment, start, end)
    stats = environment_statistics(rows)
    values = {key: night["cards"][key]["value"] for key in _CARDS}
    values.update({key: stats[key]["mean"] for key in _ENVIRONMENT})
    offsets = sleep_offsets(day, night["sleep"], now=now, zone=zone)
    if offsets is not None:
        values.update(sleep_start_offset_seconds=offsets[0], sleep_end_offset_seconds=offsets[1])
    source_ref = rows[0]["source_ref"]
    source = next(item for item in environment["sources"] if item["source_ref"] == source_ref)
    identity = (source["device_ref"], source_ref, source["placement_ref"])
    flags = set(readiness["limitations"])
    flags.update(warning["code"] for warning in readiness["warnings"])
    if any("low_light_limit" in row["metrics"]["illuminance_lux"]["quality"] for row in rows):
        flags.add("low_light_limit")
    return {
        "night_date": day.isoformat(),
        "quality": readiness["quality_state"],
        "metrics": values,
        "limits": sorted(flags),
    }, identity


def _statistics(nights):
    aggregates, comparisons = [], []
    environment_groups = sorted({night["environment_group"] for night in nights})
    for metric, unit in METRIC_UNITS.items():
        for group in environment_groups if metric in _ENVIRONMENT else (None,):
            rows = [
                night
                for night in nights
                if metric in night["metrics"]
                and (group is None or night["environment_group"] == group)
            ]
            if not rows:
                continue
            base = {"metric": metric, "unit": unit, "environment_group": group}
            aggregates.append(
                {
                    "id": f"A{len(aggregates) + 1}",
                    **base,
                    "valid_n": len(rows),
                    "median": median(night["metrics"][metric] for night in rows),
                }
            )
            poor = [night["metrics"][metric] for night in rows if night["feeling"] == "poor"]
            good = [night["metrics"][metric] for night in rows if night["feeling"] == "good"]
            if min(len(poor), len(good)) >= 2:
                comparisons.append(
                    {
                        "id": f"C{len(comparisons) + 1}",
                        **base,
                        "poor_n": len(poor),
                        "good_n": len(good),
                        "poor_median": median(poor),
                        "good_median": median(good),
                        "difference": median(poor) - median(good),
                    }
                )
    return aggregates, comparisons


def _examples(nights):
    midpoint = median(night["metrics"]["sleep_seconds"] for night in nights)
    recent = list(reversed(nights))
    poor = next(
        (n for n in recent if n["feeling"] == "poor" and n["metrics"]["sleep_seconds"] >= midpoint),
        None,
    )
    good = next(
        (n for n in recent if n["feeling"] == "good" and n["metrics"]["sleep_seconds"] < midpoint),
        None,
    )
    return list(dict.fromkeys(n["id"] for n in (poor, good, *recent) if n is not None))[:2]


# Ratings are ordinal; their distance has no quantitative meaning.
_RANK = {"poor": 0, "fair": 1, "good": 2}
PAIR_LABELS = {
    "sleep_seconds": ("睡眠时长", "更长", "更短"),
    "sleep_start_offset_seconds": ("睡眠开始时间", "更晚", "更早"),
    "sleep_end_offset_seconds": ("醒来时间", "更晚", "更早"),
    "average_heart_rate": ("夜间平均心率", "更高", "更低"),
    "night_average_hrv": ("夜间HRV", "更高", "更低"),
    "training_load": ("前日负荷", "更高", "更低"),
    "temperature_c": ("卧室温度", "更高", "更低"),
    "relative_humidity_pct": ("卧室湿度", "更高", "更低"),
    "illuminance_lux": ("卧室照度", "更高", "更低"),
    "sound_relative": ("相对声音读数", "更高", "更低"),
}


def pair_checks(nights, examples):
    """Test the illustrative pair against every equally duration-matched pair."""
    # Preserve the original example when eligible. Otherwise use the most recent
    # eligible pair, selected without looking at the sign of its difference.
    pairs = []
    for newer, older in combinations(reversed(nights), 2):
        if None in (newer["feeling"], older["feeling"]) or newer["feeling"] == older["feeling"]:
            continue
        better, worse = sorted((newer, older), key=lambda n: _RANK[n["feeling"]], reverse=True)
        ids = [better["id"], worse["id"]]
        gap = abs(better["metrics"]["sleep_seconds"] - worse["metrics"]["sleep_seconds"])
        pairs.append((ids, gap, better, worse))
    pairs.sort(key=lambda p: set(p[0]) != set(examples))
    result = []
    for index, (metric, (label, higher, lower)) in enumerate(PAIR_LABELS.items(), 1):
        eligible = []
        for ids, gap, better, worse in pairs:
            if metric not in better["metrics"] or metric not in worse["metrics"]:
                continue
            group = better["environment_group"] if metric in _ENVIRONMENT else None
            if group is not None and group != worse["environment_group"]:
                continue
            difference = better["metrics"][metric] - worse["metrics"][metric]
            direction = (difference > 0) - (difference < 0)
            eligible.append((ids, gap, group, direction))
        if not eligible:
            continue
        anchor, max_gap, group, direction = eligible[0]
        matched = [
            p for p in eligible if p[2] == group and (metric == "sleep_seconds" or p[1] <= max_gap)
        ]
        # Prefer an actual reversal over a tie when both weaken the example.
        counter = next((p for p in matched if p[3] * direction < 0), None)
        if counter is None:
            counter = next((p for p in matched if p[3] != direction), None)
        if counter:
            sentence = f"对照不同夜晚，{label}与晨起睡感的关系并不一致。"
        else:
            scope = "仅有这对夜晚可比" if len(matched) == 1 else "在这些可比夜晚中"
            detail = (
                f"睡感相对较好的那晚{label}{higher if direction > 0 else lower}"
                if direction
                else f"不同睡感对应相同的{label}"
            )
            sentence = f"{scope}，{detail}。"
        result.append(
            {
                "id": f"P{index}",
                "metric": metric,
                "example": anchor,
                "counterexample": counter[0] if counter else [],
                "max_duration_gap_seconds": None if metric == "sleep_seconds" else max_gap,
                "pair_n": len(matched),
                "night_n": len({n for p in matched for n in p[0]}),
                "text": sentence,
            }
        )
    return result


def build_evidence(nights, feeling_state):
    aggregates, comparisons = _statistics(nights)
    limits = {code for night in nights for code in night["limits"]}
    if len({night["environment_group"] for night in nights}) > 1:
        limits.add("environment_groups_differ")
    counts = Counter(night["feeling"] or "unfilled" for night in nights)
    examples = _examples(nights)
    return {
        "window": {
            "start_date": nights[0]["night_date"],
            "end_date": nights[-1]["night_date"],
            "visible_n": len(nights),
            "eligible_n": len(nights),
        },
        "quality": {
            "feeling_state": feeling_state,
            "feeling_counts": {key: counts[key] for key in _FEELINGS}
            if feeling_state == "available"
            else None,
            "limits": sorted(limits),
        },
        "aggregates": aggregates,
        "comparisons": comparisons,
        "nights": nights,
        "examples": examples,
        "pair_checks": pair_checks(nights, examples),
    }
