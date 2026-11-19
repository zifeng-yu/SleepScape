"""Versioned dual-source eligibility from existing facts; no I/O or model calls.

The approved policy requires 99% usable samples per channel and at most 300
seconds of consecutive affected minute ranges. Original counts and strict minute
coverage remain visible. Boundary statistics still belong to whole minutes.
"""

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sleepscape.domain.environment_time import summarize_intervals

POLICY_VERSION = "dual-source-coverage-1"
MIN_CHANNEL_COVERAGE = 0.99
MAX_AFFECTED_SPAN_SECONDS = 300
_MINUTE = 60_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CHANNELS = ("temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative")
_REQUIRED = (
    ("sleep_seconds", "sleep.total_minutes", "s", {"sleep", "sleep_summaries"}, False),
    ("average_heart_rate", "sleep.average_heart_rate", "bpm", {"sleep", "sleep_summaries"}, False),
    ("night_average_hrv", "sleep.night_average_hrv", "ms", {"hrv", "hrv_summaries"}, False),
    ("training_load", "previous.training_load", "load", {"activities"}, True),
    (
        "previous_stress",
        "previous.average_stress",
        "points",
        {"daily", "daily_summaries", "stress"},
        True,
    ),
)
_CONFLICTS = {
    "conflicting_duplicate_record",
    "negative_time_window",
    "sleep_summary_totals_differ",
    "invalid_elapsed_duration",
    "invalid_timestamp",
    "invalid_timestamp_unit_or_range",
}
_CHANNEL_ERRORS = {
    "missing",
    "sensor_error",
    "bus_error",
    "crc_error",
    "out_of_range",
    "partial_window",
    "below_numeric_floor",
    "audio_gap",
    "dma_overflow",
    "clipped",
    "time_unknown",
}
_SAMPLING_GAP_CODES = {"missing", "sensor_error", "partial_window", "audio_gap"}
_SEVERE_CHANNEL_CODES = {
    "bus_error",
    "crc_error",
    "out_of_range",
    "below_numeric_floor",
    "dma_overflow",
    "clipped",
    "time_unknown",
}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    return type(value) in {int, float} and math.isfinite(value)


def _count(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _codes(value):
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


def _instant(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else None
        return result.astimezone(UTC) if result and result.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _us(value):
    delta = value - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _from_us(value):
    return _iso(_EPOCH + timedelta(microseconds=value))


def _coalesce_gaps(items):
    """Compress only adjacent equivalent findings, preserving every time interval."""
    groups = {}
    for item in items:
        key = tuple(
            "numeric" if key in {"expected", "observed"} and _number(value) else value
            for key, value in item.items()
            if key not in {"window_start", "window_end", "last_checked_at"}
        )
        groups.setdefault(key, []).append(item)
    result = []
    for group in groups.values():
        merged = []
        for item in sorted(group, key=lambda row: row["window_start"] or ""):
            if merged and item["window_start"] and merged[-1]["window_end"] == item["window_start"]:
                previous = merged[-1]
                previous["window_end"] = item["window_end"]
                for key in ("expected", "observed"):
                    if _number(item[key]):
                        previous[key] += item[key]
            else:
                merged.append(dict(item))
        result.extend(merged)
    return result


def _affected_span(intervals, start, end):
    """Longest union of affected windows, clipped to sleep, without inventing bad seconds."""
    merged = []
    for left, right in sorted(intervals):
        left, right = max(left, start), min(right, end)
        if left >= right:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return max((right - left for left, right in merged), default=0) / 1e6


def _usable_channel(value, metric, per_minute, row):
    """Validate the existing wire relationships before trusting any valid sample count."""
    expected, observed, valid = (
        value.get(key) for key in ("expected_count", "observed_count", "valid_count")
    )
    if (
        not all(_count(count) for count in (expected, observed, valid))
        or expected != per_minute
        or not 0 <= valid <= observed <= expected
    ):
        return False
    if value.get("state") not in (
        {"available", "partial"} if valid else {"missing", "invalid", "unavailable", "warming"}
    ):
        return False
    if valid == expected and value.get("state") != "available":
        return False
    quality = _codes(value.get("quality"))
    allowed = _SAMPLING_GAP_CODES | (
        {"warming"} if metric == "sound_relative" else {"low_light_limit"}
    )
    if (
        quality & _SEVERE_CHANNEL_CODES
        or quality - allowed
        or _codes(row.get("quality")) & _CHANNEL_ERRORS
    ):
        return False
    if valid == expected and quality & _SAMPLING_GAP_CODES:
        return False  # A declared sampling failure with no missing sample is contradictory.
    if metric == "sound_relative":
        samples = [
            value.get(key) for key in ("expected_samples", "observed_samples", "valid_samples")
        ]
        if (
            not all(_count(count) for count in samples)
            or samples[0] != 960000
            or not valid * 16000 <= samples[2] <= samples[1] <= samples[0]
            or not observed <= samples[1] <= observed * 16000
        ):
            return False
        if any(
            not _count(value.get(key))
            for key in (
                "valid_duration_ms",
                "valid_1s_count",
                "dma_overflow_count",
                "clipped_samples",
            )
        ):
            return False
        if (
            value.get("unit") != "dBFS"
            or value["valid_duration_ms"] != valid * 1000
            or value["valid_1s_count"] != valid
            or value["dma_overflow_count"] != 0
            or value["clipped_samples"] != 0
        ):
            return False
        if not all(key in value for key in ("energy_mean_dbfs", "max_1s_rms_dbfs")):
            return False
        values = [value[key] for key in ("energy_mean_dbfs", "max_1s_rms_dbfs")]
        return (
            all(item is None for item in values)
            if valid == 0
            else all(_number(item) and -200 <= item <= 0 for item in values)
            and values[0] <= values[1]
        )
    unit, bounds = {
        "temperature_c": ("degC", (-80, 125)),
        "relative_humidity_pct": ("percent", (0, 100)),
        "illuminance_lux": ("lx", (0, 200000)),
    }[metric]
    if value.get("unit") != unit:
        return False
    if not all(key in value for key in ("min", "mean", "max")):
        return False
    values = [value[key] for key in ("min", "mean", "max")]
    return (
        all(item is None for item in values)
        if valid == 0
        else all(_number(item) for item in values)
        and bounds[0] <= values[0] <= values[1] <= values[2] <= bounds[1]
    )


def _card_state(card, day, unit, resources, previous):
    value = card.get("value")
    state = card.get("state")
    if state != "available":
        return state if state in {"stale", "invalid", "pending", "unavailable"} else "missing"
    if card.get("reason") == "latest_request_failed" or "latest_request_failed" in _codes(
        card.get("quality")
    ):
        return "stale"
    if not _number(value) or (value < 0 if previous else value <= 0):
        return "invalid"
    if card.get("source_date") != day.isoformat():
        return "date_mismatch"
    if card.get("unit") != unit or card.get("source_resource") not in resources:
        return "source_mismatch"
    source_field = {
        "s": "sleepTimeSeconds",
        "bpm": "avgHeartRate",
        "ms": "lastNightAvg",
        "load": "activityTrainingLoad",
        "points": {
            "daily_summaries": "stress.avgLevel",
            "daily": "averageStressLevel",
            "stress": "values.overallStressLevel",
        }.get(card.get("source_resource")),
    }[unit]
    if card.get("source_field") != source_field:
        return "source_mismatch"
    if _codes(card.get("quality")) & _CONFLICTS:
        return "conflict"
    if unit == "points" and value > 100:
        return "invalid"
    if unit == "load" and (
        card.get("reason")
        in {"activity_day_not_finished", "activity_scope_incomplete", "partial_activity_load"}
        or not _count(card.get("activity_count"))
        or (
            card["activity_count"] == 0
            and (value != 0 or card.get("reason") != "no_recorded_activity")
        )
    ):
        return "counts_incomplete"
    return "available"


class _Evaluation:
    def __init__(self, day, now):
        self.day, self.now = day, now
        self.checked_at = _iso(now)
        self.window = None
        self.missing = []
        self.optional = []
        self.warnings = []
        self.limitations = {"wearable_measurement_uncertain", "relative_sound_uncalibrated"}

    def gap(
        self,
        code,
        source,
        metric,
        observed,
        *,
        expected="available",
        previous=False,
        interval=None,
        recoverability=None,
        optional=False,
        warning=False,
    ):
        # Previous-day background has a calendar scope, not the current sleep window.
        window = interval if interval is not None else None if previous else self.window
        item = {
            "code": code,
            "source": source,
            "metric": metric,
            "date": (self.day - timedelta(days=int(previous))).isoformat(),
            "window_start": window["start"] if window else None,
            "window_end": window["end"] if window else None,
            "expected": expected,
            "observed": observed,
            "recoverability": recoverability or ("await_sync" if source == "garmin" else "unknown"),
            "last_checked_at": self.checked_at,
        }
        (self.optional if optional else self.warnings if warning else self.missing).append(item)

    def garmin(self, night):
        cards, sleep = _mapping(night.get("cards")), _mapping(night.get("sleep"))
        start, end = _instant(sleep.get("start_at")), _instant(sleep.get("end_at"))
        state = "available"
        if not start or not end:
            state = "missing"
        elif start >= end:
            state = "invalid"
        elif end - start > timedelta(hours=24):
            state = "too_long"
        else:
            self.window = {
                "start": _iso(start),
                "end": _iso(end),
                "duration_seconds": (end - start).total_seconds(),
            }
            if end > self.now or self.day > self.now.astimezone(ZoneInfo("Asia/Shanghai")).date():
                state = "not_closed"
        if any(sleep.get(key) is True for key in ("in_progress", "ongoing", "is_sleeping")):
            state = "not_closed"
        if sleep.get("core_conflict") is True or _codes(sleep.get("quality")) & _CONFLICTS:
            state = "conflict"
        if night.get("date") != self.day.isoformat() and night:
            state = "date_mismatch"
        if night.get("garmin_sleep_available") is False:
            state = "missing"
        summary = {
            "required_count": len(_REQUIRED),
            "available_count": 0,
            "window_state": state,
            "metrics": {},
        }
        for key, metric, unit, resources, previous in _REQUIRED:
            target = self.day - timedelta(days=int(previous))
            card = _mapping(cards.get(key))
            status = _card_state(card, target, unit, resources, previous)
            if (
                key == "sleep_seconds"
                and status == "available"
                and self.window
                and card["value"] > self.window["duration_seconds"]
            ):
                status = "invalid"
            summary["metrics"][key] = {
                "state": status,
                "source_date": target.isoformat()
                if card.get("source_date") == target.isoformat()
                else None,
            }
            if status == "available":
                summary["available_count"] += 1
            else:
                self.gap(
                    "missing_previous_day" if previous else "missing_garmin_core",
                    "garmin",
                    metric,
                    status,
                    previous=previous,
                )
        if summary["metrics"]["sleep_seconds"]["state"] == "stale" and state == "available":
            state = summary["window_state"] = "stale"
        if state != "available":
            self.gap(
                "missing_garmin_core", "garmin", "sleep.window", state, expected="closed_window"
            )
        self.garmin_optional(night, cards)
        return summary

    def garmin_optional(self, night, cards):
        checks = {
            "sleep.stage_totals": bool(night.get("stages"))
            and all(
                _mapping(row).get("state") == "available" and _number(_mapping(row).get("seconds"))
                for row in night.get("stages", [])
            ),
            "sleep.stage_timeline": _mapping(night.get("sleep")).get("stage_timeline_available")
            is True,
            "previous.activities": bool(night.get("activities")),
            "history.baseline": _mapping(night.get("load_comparison")).get("state") == "available",
        }
        for metric, key in (
            ("sleep.score", "sleep_score"),
            ("sleep.average_respiration", "average_respiration"),
            ("sleep.spo2", "spo2"),
            ("sleep.body_battery", "body_battery_at_wake"),
        ):
            card = _mapping(cards.get(key))
            checks[metric] = card.get("state") == "available" and _number(card.get("value"))
        availability = _mapping(night.get("optional_availability"))
        for metric in (
            "sleep.spo2",
            "sleep.body_battery",
            "previous.activities",
            "history.baseline",
        ):
            if type(availability.get(metric)) is bool:
                checks[metric] = availability[metric]
        for metric, available in checks.items():
            if not available:
                observed = (
                    "unsupported"
                    if metric == "sleep.stage_timeline"
                    and _mapping(night.get("sleep")).get("stage_timeline_available") is False
                    else "not_provided"
                )
                self.gap(
                    "optional_unavailable",
                    "garmin",
                    metric,
                    observed,
                    previous=metric.startswith("previous."),
                    optional=True,
                    recoverability="unknown" if observed == "unsupported" else "await_sync",
                )
        self.gap(
            "optional_unavailable", "manual", "lifestyle.context", "not_provided", optional=True
        )

    def environment(self, environment):
        if self.window is None:
            return None
        start, end = _us(_instant(self.window["start"])), _us(_instant(self.window["end"]))
        rows, identities = [], {}
        duplicate_conflict = False
        selected = environment.get("selected_source")
        for record in environment.get("records", []) or []:
            row = _mapping(record)
            if row.get("record_type") != "minute":
                continue
            if isinstance(selected, str) and row.get("source_ref") != selected:
                continue
            left, right = _instant(row.get("utc_start")), _instant(row.get("utc_end"))
            if not left or not right or left >= right:
                continue  # A record without a date cannot be attributed to this night.
            left, right = _us(left), _us(right)
            if right <= start or left >= end:
                continue
            identity = row.get("record_ref") or (
                row.get("source_ref"),
                row.get("boot_ref"),
                left,
                right,
            )
            facts = (
                left,
                right,
                row.get("duration_ms"),
                row.get("metrics"),
                row.get("quality"),
                row.get("time_quality"),
                row.get("source_ref"),
                row.get("boot_ref"),
            )
            if identity in identities:
                duplicate_conflict |= identities[identity] != facts
                continue
            identities[identity] = facts
            rows.append((left, right, row))
        source_rows = environment.get("sources", []) or []
        sources = {row.get("source_ref"): row for row in source_rows if isinstance(row, dict)}
        refs = {row.get("source_ref") for _, _, row in rows}
        valid_source = len(refs) == 1 and all(self.source_valid(sources.get(ref)) for ref in refs)
        source = sources.get(next(iter(refs))) if valid_source else None
        if rows and not valid_source:
            self.gap(
                "configuration_changed",
                "environment",
                "environment.configuration",
                "configuration_changed",
                expected="single_configuration",
            )
        if environment.get("state") != "available" or environment.get("truncated") is True:
            observed = "truncated" if environment.get("truncated") is True else "unavailable"
            self.gap(
                "missing_environment_minutes",
                "environment",
                "environment.window",
                observed,
                expected="full_coverage",
            )
        if duplicate_conflict:
            self.gap(
                "time_conflict",
                "environment",
                "environment.time",
                "conflict",
                expected="known_time",
            )
        usable, grid = [], {}
        for left, right, row in sorted(rows, key=lambda item: item[:2]):
            state = self.time_state(row)
            boot = row.get("boot_ref")
            phase = left % _MINUTE
            if not isinstance(boot, str) or not boot:
                state = "unknown"
            if boot in grid and grid[boot] != phase:
                state = "grid_mismatch"
            grid.setdefault(boot, phase)
            if right - left != _MINUTE or row.get("duration_ms") != 60000:
                state = "invalid"
            if state != "available":
                self.gap(
                    "time_conflict"
                    if state in {"conflict", "grid_mismatch"}
                    else "unknown_environment_time",
                    "environment",
                    "environment.time",
                    state,
                    expected="known_time",
                    interval={
                        "start": _from_us(max(left, start)),
                        "end": _from_us(min(right, end)),
                    },
                )
                continue
            if row.get("time_quality") == "estimated" or row.get("time_uncertainty_ms") is None:
                self.limitations.add("clock_error_unknown")
            usable.append((left, right, row))
        coverage = summarize_intervals([(left, right) for left, right, _ in usable], start, end)
        gaps = [
            {"start": _from_us(left), "end": _from_us(right), "seconds": (right - left) / 1e6}
            for left, right in coverage["gaps"]
        ]
        if coverage["overlap_us"]:
            self.gap(
                "time_conflict",
                "environment",
                "environment.time",
                "overlap",
                expected="full_coverage",
            )
        count_exact = (
            bool(usable)
            and len({row["boot_ref"] for _, _, row in usable}) == 1
            and not coverage["overlap_us"]
        )
        if count_exact:
            origin = usable[0][0]
            expected = -((origin - end) // _MINUTE) - ((start - origin) // _MINUTE)
        elif usable and not gaps and not coverage["overlap_us"]:
            expected, count_exact = len(usable), True
        else:
            expected = None
        if not valid_source:
            expected, count_exact = None, False
        lower = sum(math.ceil(interval["seconds"] / 60) for interval in gaps)
        if not count_exact:
            self.limitations.add("environment_count_lower_bound")
        global_time = _mapping(environment.get("global_time_state"))
        if gaps and any(
            _number(global_time.get(key)) and global_time[key] > 0
            for key in (
                "unknown_record_count",
                "conflict_record_count",
                "stale_source_record_count",
            )
        ):
            self.limitations.add("unknown_environment_time_possible")
            self.gap(
                "unknown_environment_time",
                "environment",
                "environment.time",
                "unknown",
                expected="known_time",
            )
        summary = {
            "expected_windows": expected,
            "observed_windows": len(usable),
            "missing_windows": max(0, expected - len(usable)) if expected is not None else None,
            "missing_windows_lower_bound": lower,
            "count_exact": count_exact,
            "expected_seconds": (end - start) / 1e6,
            "covered_seconds": coverage["covered_us"] / 1e6,
            "missing_seconds": ((end - start) - coverage["covered_us"]) / 1e6,
            "overlap_seconds": coverage["overlap_us"] / 1e6,
            "gaps": gaps,
            "channels": {},
            "coverage_scope": "fully_valid_windows_clipped_to_sleep",
        }
        if any(left < start or right > end for left, right, _ in usable):
            self.limitations.add("minute_statistics_include_boundary_outside_sleep")
        for metric in _CHANNELS:
            summary["channels"][metric] = (
                self.channel(metric, usable, source, expected, start, end, coverage["gaps"])
                if valid_source
                else None
            )
        environment_blocked = any(issue["source"] == "environment" for issue in self.missing)
        for interval in gaps:
            self.gap(
                "missing_environment_minutes",
                "environment",
                "environment.window",
                0,
                expected=interval["seconds"],
                interval=interval,
                recoverability="await_upload",
                warning=count_exact and valid_source and not environment_blocked,
            )
        event_coverage = sum(
            min(right, end) - max(left, start)
            for left, right, row in usable
            if _mapping(_mapping(row.get("metrics")).get("sound_relative")).get(
                "event_valid_duration_ms"
            )
            == 60000
        )
        if (
            event_coverage != end - start
            or coverage["overlap_us"]
            or not usable
            or not valid_source
        ):
            self.limitations.add("event_detection_incomplete")
            self.gap(
                "optional_unavailable",
                "environment",
                "environment.sound_events",
                "counts_incomplete",
                optional=True,
            )
        return summary

    @staticmethod
    def source_valid(source):
        source = _mapping(source)
        return (
            all(
                isinstance(source.get(key), str) and bool(source[key])
                for key in ("source_ref", "device_ref", "placement_ref")
            )
            and source.get("aggregation_ms") == 60000
            and source.get("sample_rate_hz") == 16000
            and all(
                _count(source.get(key)) and 0 < source[key] <= 60000 and 60000 % source[key] == 0
                for key in ("temperature_period_ms", "light_period_ms")
            )
        )

    @staticmethod
    def time_state(row):
        flags = _codes(row.get("time_flags")) | _codes(row.get("quality"))
        anchors = row.get("anchor_ids")
        if row.get("time_quality") == "conflict" or flags & {
            "anchor_conflict",
            "whole_boot_conflict",
        }:
            return "conflict"
        if (
            row.get("time_quality") not in {"estimated", "synced"}
            or not isinstance(anchors, list)
            or not anchors
            or any(not isinstance(anchor, str) or not anchor for anchor in anchors)
            or flags & {"no_usable_time_anchor", "time_unknown", "utc_out_of_range"}
        ):
            return "unknown"
        return "available"

    def channel(self, metric, rows, source, expected_windows, start, end, gaps):
        period = (
            1000
            if metric == "sound_relative"
            else source[
                "light_period_ms" if metric == "illuminance_lux" else "temperature_period_ms"
            ]
        )
        per_minute = 60000 // period
        expected = expected_windows * per_minute if expected_windows is not None else None
        observed = valid = valid_windows = usable_count = audio_missing = 0
        strict_intervals, affected, partials = [], list(gaps), []
        severe = False
        for left, right, row in rows:
            value = _mapping(_mapping(row.get("metrics")).get(metric))
            observed += value.get("observed_count") if _count(value.get("observed_count")) else 0
            valid += value.get("valid_count") if _count(value.get("valid_count")) else 0
            trusted = _usable_channel(value, metric, per_minute, row)
            strict = (
                trusted
                and value["valid_count"] == per_minute
                and value.get("state") == "available"
                and not (_codes(value.get("quality")) & _CHANNEL_ERRORS)
            )
            interval = {"start": _from_us(max(left, start)), "end": _from_us(min(right, end))}
            if trusted:
                usable_count += value["valid_count"]
            else:
                severe = True
                self.gap(
                    "invalid_channel",
                    "environment",
                    metric,
                    "invalid",
                    expected="complete_window",
                    interval=interval,
                    recoverability="unrecoverable",
                )
            if strict:
                valid_windows += 1
                strict_intervals.append((left, right))
            else:
                affected.append((left, right))
                if trusted:
                    partials.append(interval)
            if metric == "sound_relative":
                milliseconds = value.get("valid_duration_ms")
                if audio_missing is not None and _count(milliseconds) and milliseconds <= 60000:
                    audio_missing += (60000 - milliseconds) / 1000
                else:
                    audio_missing = None
        covered = summarize_intervals(strict_intervals, start, end)["covered_us"]
        max_span = _affected_span(affected, start, end)
        ratio = usable_count / expected if expected else None
        meets_coverage = (
            expected is not None and expected > 0 and usable_count * 100 >= expected * 99
        )
        meets_span = max_span <= MAX_AFFECTED_SPAN_SECONDS
        if not meets_coverage:
            self.gap(
                "insufficient_channel_coverage",
                "environment",
                metric,
                ratio,
                expected=MIN_CHANNEL_COVERAGE,
                recoverability="unknown",
            )
        if not meets_span:
            self.gap(
                "excessive_sampling_gap",
                "environment",
                metric,
                max_span,
                expected=MAX_AFFECTED_SPAN_SECONDS,
                recoverability="unknown",
            )
        for interval in partials:
            self.gap(
                "invalid_channel",
                "environment",
                metric,
                "counts_incomplete",
                expected="complete_window",
                interval=interval,
                recoverability="unrecoverable",
                warning=meets_coverage and meets_span and not severe,
            )
        result = {
            "expected_windows": expected_windows,
            "valid_windows": valid_windows,
            "invalid_windows": len(rows) - valid_windows,
            "expected_count": expected,
            "observed_count": observed,
            "valid_count": valid,
            "missing_count": max(0, expected - valid) if expected is not None else None,
            "coverage_ratio": covered / (end - start),
            "missing_seconds": (end - start - covered) / 1e6,
            "effective_coverage_ratio": ratio,
            "usable_count": usable_count,
            "max_affected_span_seconds": max_span,
        }
        if metric == "sound_relative":
            result["audio_missing_seconds"] = audio_missing
            result["audio_missing_scope"] = "whole_observed_minutes"
        return result


def evaluate_readiness(
    day: date,
    night: dict | None,
    environment: dict | None,
    *,
    now: datetime,
    lookback_days: int = 7,
) -> dict:
    """Evaluate an immutable snapshot, including expired dates without changing facts."""
    if type(day) is not date or not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("readiness_requires_date_and_aware_now")
    if type(lookback_days) is not int or not 1 <= lookback_days <= 31:
        raise ValueError("invalid_readiness_lookback")
    evaluation = _Evaluation(day, now)
    garmin = evaluation.garmin(_mapping(night))
    environment_summary = evaluation.environment(_mapping(environment))
    ready = not evaluation.missing
    expired = day < now.astimezone(ZoneInfo("Asia/Shanghai")).date() - timedelta(
        days=lookback_days - 1
    )
    return {
        "policy_version": POLICY_VERSION,
        "policy": {
            "min_channel_coverage": MIN_CHANNEL_COVERAGE,
            "max_affected_span_seconds": MAX_AFFECTED_SPAN_SECONDS,
        },
        "night_date": day.isoformat(),
        "checked_at": evaluation.checked_at,
        "data_state": "expired_unanalyzed" if expired else "ready" if ready else "waiting_data",
        "core_ready": ready,
        "quality_state": "insufficient"
        if not ready
        else "with_gaps"
        if evaluation.warnings
        else "complete",
        "missing": _coalesce_gaps(evaluation.missing),
        "warnings": _coalesce_gaps(evaluation.warnings),
        "optional_missing": _coalesce_gaps(evaluation.optional),
        "garmin_summary": garmin,
        "environment_summary": environment_summary,
        "limitations": sorted(evaluation.limitations),
        "window": evaluation.window,
    }
