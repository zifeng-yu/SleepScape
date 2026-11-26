"""Pure bounded anonymous sleep evidence, numerical preparation and semantic validation."""

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime, timedelta

from sleepscape.domain.analysis_readiness import (
    LIGHT_BUS_WARNING,
    MAX_LIGHT_BUS_LOST_SAMPLES,
    POLICY_VERSION,
    READABLE_COVERAGE_POLICIES,
    _usable_channel,
    evaluate_readiness,
)
from sleepscape.domain.morning_feeling import Feeling

MAX_INPUT_BYTES = 24 * 1024
MORNING_FEELING_EVIDENCE_ID = "F000"
CHANNELS = ("temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative")
CORE_METRICS = (
    "sleep.total_minutes",
    "sleep.average_heart_rate",
    "sleep.night_average_hrv",
    "previous.training_load",
    "previous.average_stress",
    "environment.temperature_mean",
    "environment.humidity_mean",
    "environment.illuminance_mean",
    "environment.sound_energy_mean",
)
# unit, source, period, minimum, maximum; never accept caller-supplied free text metrics.
FACT_REGISTRY = {
    "sleep.total_minutes": ("min", "garmin", "night", 0, 1440),
    "sleep.average_heart_rate": ("bpm", "garmin", "night", 1, 300),
    "sleep.night_average_hrv": ("ms", "garmin", "night", 0.001, 1000),
    "previous.training_load": ("load_units", "garmin", "previous_day", 0, 1000000),
    "previous.average_stress": ("points", "garmin", "previous_day", 0, 100),
    "previous.steps": ("count", "garmin", "previous_day", 0, 1000000),
    "sleep.score": ("points", "garmin", "night", 0, 100),
    "sleep.body_battery_at_wake": ("points", "garmin", "night", 0, 100),
}
for _stage in ("deep", "light", "rem", "awake", "unmeasurable"):
    FACT_REGISTRY[f"sleep.{_stage}_minutes"] = ("min", "garmin", "night", 0, 1440)
for _metric, _unit, _low, _high in (
    ("temperature", "degC", -80, 125),
    ("humidity", "percent", 0, 100),
    ("illuminance", "lx", 0, 200000),
    ("sound_energy", "dBFS", -200, 0),
):
    for _stat in ("mean",) if _metric == "sound_energy" else ("min", "mean", "max"):
        FACT_REGISTRY[f"environment.{_metric}_{_stat}"] = (
            _unit,
            "environment",
            "night",
            _low,
            _high,
        )
FACT_REGISTRY["environment.sound_peak"] = ("dBFS", "environment", "night", -200, 0)
for _metric, _unit, _high in (
    ("sleep_total_minutes", "min", 1440),
    ("average_heart_rate", "bpm", 300),
    ("night_average_hrv", "ms", 1000),
):
    FACT_REGISTRY[f"baseline.{_metric}"] = (_unit, "derived", "baseline30", 0, _high)
    FACT_REGISTRY[f"comparison.{_metric}_delta"] = (_unit, "derived", "comparison", -_high, _high)
FACT_REGISTRY["baseline.training_load"] = ("load_units", "derived", "baseline30", 0, 1000000)
FACT_REGISTRY["comparison.training_load_percent"] = (
    "percent",
    "derived",
    "comparison",
    -100,
    1000000000,
)
LIMITATIONS = frozenset(
    {
        "wearable_estimates",
        "relative_sound_only",
        "no_stage_timeline",
        "unobserved_lifestyle",
        "clock_error_unknown",
        "baseline_insufficient",
        "sampling_gaps_present",
        "event_detection_incomplete",
        "event_context_truncated",
        "minute_boundary_estimate",
        "timeline_omitted_for_budget",
        "optional_context_omitted_for_budget",
        "composite_metrics_not_independent",
        "single_night_not_causal",
    }
)
_COVERAGE_FIELDS = (
    "expected_windows",
    "valid_windows",
    "invalid_windows",
    "expected_count",
    "observed_count",
    "valid_count",
    "usable_count",
    "effective_coverage_ratio",
    "coverage_ratio",
    "max_affected_span_seconds",
)
_WARNING_FIELDS = (
    "code",
    "source",
    "metric",
    "window_start",
    "window_end",
    "expected",
    "observed",
    "recoverability",
)


class SleepAnalysisError(ValueError):
    def __init__(self, code):
        self.code = (
            code
            if code
            in {
                "invalid_analysis_input",
                "invalid_analysis_output",
                "blocked_input_budget",
                "analysis_not_ready",
                "model_insufficient_input",
            }
            else "invalid_analysis_input"
        )
        super().__init__(self.code)


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def input_hash(payload):
    return hashlib.sha256(
        canonical_json(
            {
                k: v
                for k, v in payload.items()
                if k not in {"input_hash", "data_cutoff_at", "is_provisional"}
            }
        ).encode()
    ).hexdigest()


def _instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("timezone_required")
    return result.astimezone(UTC)


def _stamp(value):
    return _instant(value).isoformat().replace("+00:00", "Z")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _assert(condition):
    if not condition:
        raise SleepAnalysisError("invalid_analysis_input")


def _coverage(channels):
    return {
        metric: {
            "count_unit": "one_second_windows" if metric == "sound_relative" else "sample_points",
            **{k: value[k] for k in _COVERAGE_FIELDS},
        }
        for metric, value in channels.items()
    }


def _fact(metric, value, valid_n=None, expected_n=None):
    unit, source, period, low, high = FACT_REGISTRY[metric]
    _assert(value is None or (_number(value) and low <= value <= high))
    return {
        "metric": metric,
        "value": value,
        "unit": unit,
        "source": source,
        "period": period,
        "quality": "unavailable" if value is None else "valid",
        "valid_n": valid_n,
        "expected_n": expected_n,
    }


def environment_minutes(environment, start, end):
    return sorted(
        (
            r
            for r in environment["records"]
            if r["record_type"] == "minute"
            and _instant(r["utc_start"]) < end
            and _instant(r["utc_end"]) > start
        ),
        key=lambda r: (_instant(r["utc_start"]), r["record_ref"]),
    )


def environment_statistics(rows):
    result = {}
    for metric in CHANNELS:
        points = []
        for row in rows:
            v = row["metrics"][metric]
            _assert(_usable_channel(v, metric, v.get("expected_count"), row))
            if v["valid_count"]:
                points.append(v)
        weight = sum(v["valid_count"] for v in points)
        if not weight:
            result[metric] = {"mean": None, "min": None, "max": None, "usable_count": 0}
            continue
        sound = metric == "sound_relative"
        mean = (
            sum(
                (10 ** (v["energy_mean_dbfs"] / 10) if sound else v["mean"]) * v["valid_count"]
                for v in points
            )
            / weight
        )
        result[metric] = {
            "mean": 10 * math.log10(mean) if sound else mean,
            "min": None if sound else min(v["min"] for v in points),
            "max": max(v["max_1s_rms_dbfs" if sound else "max"] for v in points),
            "usable_count": weight,
        }
    return result


def _events(environment, rows, start, end, summary):
    events = []
    incomplete = False
    for row in environment["records"]:
        if row["record_type"] != "event":
            continue
        left, right = _instant(row["utc_start"]), _instant(row["utc_end"])
        if right <= start or left >= end:
            continue
        event = row["event"]
        if (
            left < start
            or right > end
            or set(row.get("quality", [])) - {"merged"}
            or row.get("source_ref") not in {r["source_ref"] for r in rows}
            or row.get("boot_ref") not in {r["boot_ref"] for r in rows}
            or row.get("time_quality") not in {"estimated", "synced"}
            or event.get("event_kind") != "relative_sound"
            or event.get("incomplete")
            or not left < right
            or not _number(event.get("max_1s_rms_dbfs"))
            or not -200 <= event["max_1s_rms_dbfs"] <= 0
            or not _number(event.get("active_duration_ms"))
            or not 0 < event["active_duration_ms"] <= (right - left).total_seconds() * 1000
        ):
            incomplete = True
            continue
        events.append(
            {
                "start_at": _stamp(left),
                "end_at": _stamp(right),
                "profile_segment": "segment-1",
                "active_seconds": event["active_duration_ms"] / 1000,
                "max_1s_rms_dbfs": event["max_1s_rms_dbfs"],
                "merged": event["merged"],
                "incomplete": False,
            }
        )
    stored_counts = [r["metrics"]["sound_relative"].get("stored_count") for r in rows]
    counts_known = all(type(value) is int and value >= 0 for value in stored_counts)
    counters_agree = counts_known and sum(stored_counts) == len(events)
    detector_complete = (
        counters_agree
        and summary["missing_windows"] == 0
        and all(
            r["metrics"]["sound_relative"].get("event_detector_state") == "ready"
            and r["metrics"]["sound_relative"].get("event_valid_duration_ms") == 60000
            and r["metrics"]["sound_relative"].get("valid_duration_ms") == 60000
            and r["metrics"]["sound_relative"].get("dropped_count") == 0
            for r in rows
        )
    )
    if len(events) > 8:
        events = sorted(events, key=lambda x: (-x["max_1s_rms_dbfs"], x["start_at"]))[:8]
        status = "context_truncated"
    elif incomplete or not detector_complete:
        status = "context_truncated" if events else "unavailable"
    else:
        status = "available" if events else "none_detected"
    events.sort(key=lambda x: (x["start_at"], x["end_at"]))
    return [{"id": f"E{i:03}", **item} for i, item in enumerate(events, 1)], status


def build_sleep_input(
    day,
    night,
    environment,
    readiness,
    *,
    data_cutoff_at,
    synthetic=False,
    morning_feeling=None,
    allow_expired=False,
):
    """Construct a bounded anonymous snapshot; neither source values nor the gate are changed."""
    try:
        state = readiness.get("data_state")
        if state != "ready" and not (allow_expired and state == "expired_unanalyzed"):
            raise SleepAnalysisError("analysis_not_ready")
        return _build(
            day,
            night,
            environment,
            readiness,
            data_cutoff_at,
            synthetic,
            morning_feeling,
        )
    except SleepAnalysisError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise SleepAnalysisError("invalid_analysis_input") from None


def _build(day, night, environment, readiness, cutoff, synthetic, morning_feeling):
    _assert(type(day) is date and type(synthetic) is bool)
    cutoff = _instant(cutoff)
    if readiness.get("core_ready") is not True:
        raise SleepAnalysisError("analysis_not_ready")
    actual = evaluate_readiness(day, night, environment, now=cutoff, lookback_days=31)
    for key in (
        "core_ready",
        "quality_state",
        "policy_version",
        "policy",
        "window",
        "environment_summary",
    ):
        _assert(actual[key] == readiness[key])
    _assert(actual["core_ready"] and actual["night_date"] == day.isoformat())
    start, end = _instant(actual["window"]["start"]), _instant(actual["window"]["end"])
    rows = environment_minutes(environment, start, end)
    stats = environment_statistics(rows)
    summary = actual["environment_summary"]
    channels = summary["channels"]
    facts = []
    for metric, card, scale in (
        (CORE_METRICS[0], "sleep_seconds", 60),
        (CORE_METRICS[1], "average_heart_rate", 1),
        (CORE_METRICS[2], "night_average_hrv", 1),
        (CORE_METRICS[3], "training_load", 1),
        (CORE_METRICS[4], "previous_stress", 1),
    ):
        facts.append(_fact(metric, night["cards"][card]["value"] / scale))
    for metric, name in zip(
        CHANNELS, ("temperature", "humidity", "illuminance", "sound_energy"), strict=True
    ):
        _assert(stats[metric]["usable_count"] == channels[metric]["usable_count"])
        facts.append(
            _fact(
                f"environment.{name}_mean",
                stats[metric]["mean"],
                channels[metric]["usable_count"],
                channels[metric]["expected_count"],
            )
        )
    for metric, name in zip(
        CHANNELS, ("temperature", "humidity", "illuminance", "sound_energy"), strict=True
    ):
        fields = (
            [("sound_peak", "max")]
            if metric == "sound_relative"
            else [(f"{name}_{s}", s) for s in ("min", "max")]
        )
        for label, field in fields:
            facts.append(
                _fact(
                    f"environment.{label}",
                    stats[metric][field],
                    channels[metric]["usable_count"],
                    channels[metric]["expected_count"],
                )
            )
    for item in night.get("stages", []):
        if (
            item.get("stage") in {"deep", "light", "rem", "awake", "unmeasurable"}
            and item.get("state") == "available"
            and _number(item.get("seconds"))
            and 0 <= item["seconds"] <= (end - start).total_seconds()
        ):
            facts.append(_fact(f"sleep.{item['stage']}_minutes", item["seconds"] / 60))
    for name, card_name in (
        ("sleep.score", "sleep_score"),
        ("sleep.body_battery_at_wake", "body_battery_at_wake"),
        ("previous.steps", "previous_steps"),
    ):
        card = night.get("cards", {}).get(card_name, {})
        if (
            card.get("state") == "available"
            and _number(card.get("value"))
            and card.get("source_date")
            == (day - timedelta(days=int(name.startswith("previous.")))).isoformat()
            and FACT_REGISTRY[name][3] <= card["value"] <= FACT_REGISTRY[name][4]
        ):
            facts.append(_fact(name, card["value"]))
    limitations = {
        "wearable_estimates",
        "relative_sound_only",
        "unobserved_lifestyle",
        "single_night_not_causal",
    }
    if any(f["metric"] in {"sleep.score", "sleep.body_battery_at_wake"} for f in facts):
        limitations.add("composite_metrics_not_independent")
    if not night.get("sleep", {}).get("stage_timeline_available"):
        limitations.add("no_stage_timeline")
    if "clock_error_unknown" in actual["limitations"]:
        limitations.add("clock_error_unknown")
    if actual["quality_state"] == "with_gaps":
        limitations.add("sampling_gaps_present")
    if any(_instant(r["utc_start"]) < start or _instant(r["utc_end"]) > end for r in rows):
        limitations.add("minute_boundary_estimate")
    events, event_status = _events(environment, rows, start, end, summary)
    if event_status == "context_truncated":
        limitations.add("event_context_truncated")
    if event_status == "unavailable":
        limitations.add("event_detection_incomplete")
    payload = {
        "schema_version": "sleep-input-1",
        "synthetic": synthetic,
        "night_date": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "input_hash": "0" * 64,
        "data_cutoff_at": _stamp(cutoff),
        "is_provisional": False,
        "readiness": "ready",
        "sleep_window": {"start_at": _stamp(start), "end_at": _stamp(end)},
        "completeness": {
            "policy_version": POLICY_VERSION,
            "policy": actual["policy"],
            "quality_state": actual["quality_state"],
            "garmin_core_complete": True,
            "environment_eligible": True,
            "environment_expected_windows": summary["expected_windows"],
            "environment_observed_windows": summary["observed_windows"],
            "time_quality": "estimated"
            if any(r["time_quality"] == "estimated" for r in rows)
            else "anchored",
            "channels": _coverage(channels),
            "warnings": [{k: r[k] for k in _WARNING_FIELDS} for r in actual["warnings"]],
            "warning_interval_scope": "affected_minute_ranges_clipped_to_sleep",
        },
        "facts": [{"id": f"F{i:03}", **item} for i, item in enumerate(facts, 1)],
        "timeline": [],
        "events": events,
        "event_status": event_status,
        "required_limitations": sorted(limitations),
    }
    if morning_feeling is not None:
        payload["morning_feeling"] = {
            "id": MORNING_FEELING_EVIDENCE_ID,
            "value": Feeling(morning_feeling).value,
        }
    if len(canonical_json(payload).encode()) > MAX_INPUT_BYTES:
        payload["facts"] = [f for f in payload["facts"] if f["metric"] in CORE_METRICS]
        limitations.add("optional_context_omitted_for_budget")
        payload["required_limitations"] = sorted(limitations)
    if len(canonical_json(payload).encode()) > MAX_INPUT_BYTES:
        raise SleepAnalysisError("blocked_input_budget")
    payload["input_hash"] = input_hash(payload)
    validate_input_semantics(payload)
    return payload


def _validate_coverage(v, eligible=False):
    counts = [v[k] for k in ("usable_count", "valid_count", "observed_count", "expected_count")]
    _assert(
        all(type(n) is int and n >= 0 for n in counts)
        and counts[-1] > 0
        and counts == sorted(counts)
    )
    _assert(
        math.isclose(
            v["effective_coverage_ratio"], v["usable_count"] / v["expected_count"], abs_tol=1e-12
        )
    )
    _assert(
        v["valid_windows"] + v["invalid_windows"] <= v["expected_windows"]
        and 0 <= v["coverage_ratio"] <= 1
    )
    if eligible:
        _assert(
            v["usable_count"] * 100 >= v["expected_count"] * 99
            and v["max_affected_span_seconds"] <= 300
        )


def validate_input_semantics(payload):
    try:
        _validate_input(payload)
    except SleepAnalysisError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise SleepAnalysisError("invalid_analysis_input") from None


_COMPARISONS = {
    "comparison.sleep_total_minutes_delta": ("sleep.total_minutes", "睡眠时长", "长于", "短于"),
    "comparison.average_heart_rate_delta": (
        "sleep.average_heart_rate",
        "夜间平均心率",
        "高于",
        "低于",
    ),
    "comparison.night_average_hrv_delta": ("sleep.night_average_hrv", "夜间HRV", "高于", "低于"),
    "comparison.training_load_percent": ("previous.training_load", "前日负荷", "高于", "低于"),
}


def comparison_phrase(metric, delta):
    if metric not in _COMPARISONS or delta is None:
        return None
    _, name, up, down = _COMPARISONS[metric]
    return name + (up if delta > 0 else down if delta < 0 else "等于") + "个人均值"


def _validate_comparison(fact, expected):
    _assert(math.isclose(fact["value"], expected, abs_tol=1e-9))
    if "comparison_text" in fact:
        _assert(fact["comparison_text"] == comparison_phrase(fact["metric"], expected))


def _validate_input(p):
    _assert(len(canonical_json(p).encode()) <= MAX_INPUT_BYTES and p["input_hash"] == input_hash(p))
    start, end = (_instant(p["sleep_window"][k]) for k in ("start_at", "end_at"))
    _assert(
        timedelta(0) < end - start <= timedelta(hours=24) and end <= _instant(p["data_cutoff_at"])
    )
    date.fromisoformat(p["night_date"])
    c = p["completeness"]
    _assert(
        c["policy_version"] in READABLE_COVERAGE_POLICIES
        and c["policy"] == {"min_channel_coverage": 0.99, "max_affected_span_seconds": 300}
    )
    _assert(
        c["environment_observed_windows"] <= c["environment_expected_windows"]
        and c["quality_state"] in {"complete", "with_gaps"}
        and (c["quality_state"] == "with_gaps") == bool(c["warnings"])
    )
    for metric in CHANNELS:
        v = c["channels"][metric]
        _validate_coverage(v, True)
        _assert(v["expected_windows"] == c["environment_expected_windows"])
    if c["quality_state"] == "complete":
        _assert(c["environment_observed_windows"] == c["environment_expected_windows"])
        _assert(
            all(
                v["usable_count"] == v["expected_count"]
                and v["invalid_windows"] == 0
                and v["max_affected_span_seconds"] == 0
                for v in c["channels"].values()
            )
        )
    _assert(
        all(
            v["valid_windows"] + v["invalid_windows"] == c["environment_observed_windows"]
            for v in c["channels"].values()
        )
    )
    ids = []
    metrics = {}
    for f in p["facts"]:
        metric = f["metric"]
        _assert(metric in FACT_REGISTRY and metric not in metrics)
        unit, source, period, low, high = FACT_REGISTRY[metric]
        value = f["value"]
        _assert((f["unit"], f["source"], f["period"]) == (unit, source, period))
        _assert(
            (value is None and f["quality"] == "unavailable")
            or (_number(value) and low <= value <= high and f["quality"] == "valid")
        )
        if "comparison_text" in f:
            _assert(f["comparison_text"] == comparison_phrase(metric, value))
        valid, expected = f["valid_n"], f["expected_n"]
        _assert(
            (valid is None and expected is None)
            or (type(valid) is int and type(expected) is int and 0 <= valid <= expected)
        )
        if period == "baseline30":
            _assert(
                expected == 30
                and (value is None or valid >= (30 if metric == "baseline.training_load" else 7))
            )
        metrics[metric] = f
        ids.append(f["id"])
    _assert(all(name in metrics and metrics[name]["quality"] == "valid" for name in CORE_METRICS))
    _assert(metrics["sleep.total_minutes"]["value"] * 60 <= (end - start).total_seconds() + 1e-7)
    for metric, name in zip(
        CHANNELS, ("temperature", "humidity", "illuminance", "sound_energy"), strict=True
    ):
        f = metrics[f"environment.{name}_mean"]
        v = c["channels"][metric]
        _assert(f["valid_n"] == v["usable_count"] and f["expected_n"] == v["expected_count"])
    for name, current in (
        ("sleep_total_minutes", "sleep.total_minutes"),
        ("average_heart_rate", "sleep.average_heart_rate"),
        ("night_average_hrv", "sleep.night_average_hrv"),
    ):
        delta = metrics.get(f"comparison.{name}_delta")
        if delta:
            base = metrics.get(f"baseline.{name}", {})
            _assert(base.get("quality") == "valid")
            _assert(delta["valid_n"] == base["valid_n"] and delta["expected_n"] == 30)
            _validate_comparison(delta, metrics[current]["value"] - base["value"])
    change = metrics.get("comparison.training_load_percent")
    if change:
        base = metrics.get("baseline.training_load", {})
        _assert(base.get("quality") == "valid" and base["value"] > 0)
        _assert(change["valid_n"] == 30 and change["expected_n"] == 30)
        expected = (metrics["previous.training_load"]["value"] / base["value"] - 1) * 100
        _validate_comparison(change, expected)
    for name in ("temperature", "humidity", "illuminance"):
        mean = metrics[f"environment.{name}_mean"]["value"]
        low = metrics.get(f"environment.{name}_min", {}).get("value", mean)
        high = metrics.get(f"environment.{name}_max", {}).get("value", mean)
        _assert(low <= mean <= high)
    previous = start
    for row in p["timeline"]:
        left, right = _instant(row["start_at"]), _instant(row["end_at"])
        _assert(previous <= left < right <= end and right - left <= timedelta(minutes=15))
        previous = right
        for metric in CHANNELS:
            v = row["coverage"][metric]
            _validate_coverage(v)
            column = {"relative_humidity_pct": "humidity_pct", "sound_relative": "sound_dbfs"}.get(
                metric, metric
            )
            _assert((row[column] is None) == (v["usable_count"] == 0))
        ids.append(row["id"])
    for row in p["events"]:
        left, right = _instant(row["start_at"]), _instant(row["end_at"])
        _assert(
            start <= left < right <= end
            and 0 < row["active_seconds"] <= (right - left).total_seconds()
        )
        ids.append(row["id"])
    if "morning_feeling" in p:
        feeling = p["morning_feeling"]
        _assert(
            set(feeling) == {"id", "value"}
            and feeling["id"] == MORNING_FEELING_EVIDENCE_ID
            and feeling["value"] in tuple(Feeling)
        )
        ids.append(feeling["id"])
    _assert(len(ids) == len(set(ids)))
    for row in c["warnings"]:
        _assert(start <= _instant(row["window_start"]) < _instant(row["window_end"]) <= end)
        if row["code"] == LIGHT_BUS_WARNING:
            _assert(
                c["policy_version"] == POLICY_VERSION
                and row["metric"] == "illuminance_lux"
                and row["recoverability"] == "unrecoverable"
                and type(row["expected"]) is int
                and row["expected"] > 0
                and row["expected"] % 60 == 0
                and type(row["observed"]) is int
            )
            windows = row["expected"] // 60
            _assert(
                windows <= row["expected"] - row["observed"] <= MAX_LIGHT_BUS_LOST_SAMPLES * windows
            )
    limits = set(p["required_limitations"])
    _assert(
        limits <= LIMITATIONS
        and {"wearable_estimates", "relative_sound_only", "unobserved_lifestyle"} <= limits
    )
    _assert(c["quality_state"] != "with_gaps" or "sampling_gaps_present" in limits)
    _assert(c["time_quality"] != "estimated" or "clock_error_unknown" in limits)
    _assert(p["event_status"] not in {"none_detected", "unavailable"} or not p["events"])
    _assert(p["event_status"] != "context_truncated" or "event_context_truncated" in limits)
    _assert(p["event_status"] != "unavailable" or "event_detection_incomplete" in limits)


def report_text_is_safe(text):
    # Measurements live in immutable evidence cards; generated prose does not do arithmetic.
    if re.search(
        r"(?<!SpO)\d|https?://|www\.|<[^>]*>|sk-|GCDC_|忽略.{0,12}(指令|规则)|system\s*:",
        text,
        re.I,
    ):
        return False
    # These complete clauses express measurement limits or record unknown context.
    # Boundaries prevent embedded/double negations; the remaining text is still
    # checked for medication instructions, causal rankings and all other bans.
    sound_subject = r"(?:声音|声音读数|相对声音读数|这些读数)?"
    safe_clause = (
        r"(?:目前|现有证据)?无法判断哪一项影响最大"
        rf"|{sound_subject}(?:不是|并非|不能换算为)(?:dBA|分贝A)"
        r"|用药情况(?:未知|未记录)"
        r"|未记录(?:用药情况|是否服用药物)"
        r"|(?:请|建议)?记录用药情况"
        r"|不提供(?:用药建议(?:[或和/]治疗方案)?|治疗方案)"
        r"|这些数据不能用于确诊"
    )
    guarded_text = re.sub(
        rf"(^|[。！？；，.!?;,\n])(?:{safe_clause})(?=$|[。！？；，.!?;,\n])",
        r"\1",
        text,
        flags=re.I,
    )
    if re.search(
        r"dBA|分贝A|毫克|用药|服用|药物剂量|治疗方案|诊断为|确诊|致病|病因是|因果占比|影响最大|主要原因是",
        guarded_text,
        re.I,
    ):
        return False
    return True


def opens_with_collection_status(text):
    # Only screen obvious collection-status leads in new answers. A later
    # caveat about how a gap limits a sleep interpretation is legitimate.
    lead = re.split(r"[，,。！？；;：:\n]", text.strip(), maxsplit=1)[0]
    return bool(
        re.search(
            r"(?:数据|采样|采集|记录|指标|观测)(?:的)?(?:完整[度性]|覆盖率)|"
            r"采样率|通过.{0,6}(?:核验|校验)|缺采|"
            r"(?:数据|采样|采集|记录|指标|观测)(?:已|均|都|基本|较为|相对|较|很|十分|比较|总体)*"
            r"(?:齐全|齐备|完整|达标|可用|缺失|覆盖充分|可供记录)",
            lead,
        )
    )


def validate_output_semantics(payload, input_payload):
    try:
        _validate_output(payload, input_payload)
    except SleepAnalysisError as exc:
        if exc.code == "model_insufficient_input":
            raise
        raise SleepAnalysisError("invalid_analysis_output") from None
    except (ValueError, TypeError, KeyError, RecursionError):
        raise SleepAnalysisError("invalid_analysis_output") from None


def _validate_output(p, source):
    _assert(all(p[k] == source[k] for k in ("night_date", "input_hash")))
    _assert(
        p["input_quality_state"] == source["completeness"]["quality_state"]
        and set(p["limitations"]) == set(source["required_limitations"])
    )
    if p["status"] == "insufficient_input":
        _assert(not p["observations"] and not p["hypotheses"] and not p["actions"])
        raise SleepAnalysisError("model_insufficient_input")
    evidence = {r["id"]: r for group in ("facts", "timeline", "events") for r in source[group]}
    if feeling := source.get("morning_feeling"):
        evidence[feeling["id"]] = feeling
    _assert(
        {r["category"] for r in p["observations"]}
        == {"sleep", "body_signals", "previous_day", "environment"}
    )
    _assert(bool(p["summary"]["evidence_ids"]))
    for claim in [p["summary"], *p["observations"], *p["hypotheses"], *p["actions"]]:
        _assert(bool(claim["evidence_ids"]) and set(claim["evidence_ids"]) <= set(evidence))
        _assert(all(evidence[i].get("quality") != "unavailable" for i in claim["evidence_ids"]))
        texts = [claim["text"], *claim.get("alternatives", [])] + (
            [claim["observe"]] if "observe" in claim else []
        )
        _assert(all(report_text_is_safe(t) for t in texts))
    for row in p["observations"]:
        facts = [evidence[i].get("metric", "") for i in row["evidence_ids"]]
        supported = {
            "sleep": any(f.startswith("sleep.") and f.endswith("minutes") for f in facts),
            "body_signals": any(
                f in {"sleep.average_heart_rate", "sleep.night_average_hrv"} for f in facts
            ),
            "previous_day": any(f.startswith("previous.") for f in facts),
            "environment": any(f.startswith("environment.") for f in facts)
            or any(i.startswith(("T", "E")) for i in row["evidence_ids"]),
        }
        _assert(supported[row["category"]])
    for row in p["hypotheses"]:
        prefix, bin_key = {
            "temperature": ("environment.temperature_", "temperature_c"),
            "humidity": ("environment.humidity_", "humidity_pct"),
            "light": ("environment.illuminance_", "illuminance_lux"),
            "sound": ("environment.sound_", "sound_dbfs"),
            "training_load": ("previous.training_load", None),
            "stress": ("previous.average_stress", None),
            "sleep_schedule": ("sleep.total_minutes", None),
        }[row["factor"]]
        _assert(
            any(
                evidence[i].get("metric", "").startswith(prefix)
                or (i.startswith("T") and bin_key and evidence[i].get(bin_key) is not None)
                or (i.startswith("E") and row["factor"] == "sound")
                for i in row["evidence_ids"]
            )
        )
    if p["input_quality_state"] == "with_gaps":
        text = p["summary"]["text"] + "".join(
            r["text"] for r in p["observations"] if r["category"] == "environment"
        )
        _assert(any(w in text for w in ("缺采", "缺样", "缺口", "漏采", "缺失")))
