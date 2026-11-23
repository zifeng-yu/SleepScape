"""Synthetic coverage-policy boundaries; no clock, database, network or model calls."""

import copy
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from sleepscape.domain.analysis_readiness import evaluate_readiness

DAY = date(2026, 1, 2)
START = datetime(2026, 1, 1, 22, tzinfo=UTC)
NOW = datetime(2026, 1, 2, 3, tzinfo=UTC)


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def sample(minutes=3):
    def card(value, unit, resource, previous=False, **extra):
        return {
            "value": value,
            "unit": unit,
            "state": "available",
            "source_date": (DAY - timedelta(days=int(previous))).isoformat(),
            "source_resource": resource,
            "source_field": {
                "s": "sleepTimeSeconds",
                "bpm": "avgHeartRate",
                "ms": "lastNightAvg",
                "load": "activityTrainingLoad",
                "points": "stress.avgLevel",
            }[unit],
            "quality": [],
            **extra,
        }

    night = {
        "date": DAY.isoformat(),
        "garmin_sleep_available": True,
        "sleep": {"start_at": stamp(START), "end_at": stamp(START + timedelta(minutes=minutes))},
        "cards": {
            "sleep_seconds": card(minutes * 60, "s", "sleep"),
            "average_heart_rate": card(55, "bpm", "sleep"),
            "night_average_hrv": card(45, "ms", "hrv"),
            "training_load": card(
                0, "load", "activities", True, activity_count=0, reason="no_recorded_activity"
            ),
            "previous_stress": card(20, "points", "daily_summaries", True),
        },
    }
    source = {
        "source_ref": "source-synthetic",
        "device_ref": "device-synthetic",
        "placement_ref": "placement-synthetic",
        "sample_rate_hz": 16000,
        "temperature_period_ms": 10000,
        "light_period_ms": 1000,
        "aggregation_ms": 60000,
    }

    def scalar(value, unit, count):
        return {
            "unit": unit,
            "state": "available",
            "expected_count": count,
            "observed_count": count,
            "valid_count": count,
            "mean": value,
            "min": value,
            "max": value,
            "quality": [],
        }

    metrics = {
        "temperature_c": scalar(20, "degC", 6),
        "relative_humidity_pct": scalar(45, "percent", 6),
        "illuminance_lux": scalar(0, "lx", 60),
        "sound_relative": {
            "unit": "dBFS",
            "state": "available",
            "expected_count": 60,
            "observed_count": 60,
            "valid_count": 60,
            "energy_mean_dbfs": -45,
            "max_1s_rms_dbfs": -40,
            "expected_samples": 960000,
            "observed_samples": 960000,
            "valid_samples": 960000,
            "valid_duration_ms": 60000,
            "valid_1s_count": 60,
            "dma_overflow_count": 0,
            "clipped_samples": 0,
            "quality": [],
            "event_detector_state": "ready",
            "event_valid_duration_ms": 60000,
        },
    }
    environment = {
        "state": "available",
        "truncated": False,
        "sources": [source],
        "records": [],
        "global_time_state": {"unknown_record_count": 0, "conflict_record_count": 0},
    }
    for i in range(minutes):
        environment["records"].append(
            {
                "record_ref": f"record-{i}",
                "record_type": "minute",
                "source_ref": source["source_ref"],
                "boot_ref": "boot-synthetic",
                "utc_start": stamp(START + timedelta(minutes=i)),
                "utc_end": stamp(START + timedelta(minutes=i + 1)),
                "duration_ms": 60000,
                "time_quality": "estimated",
                "time_uncertainty_ms": None,
                "anchor_ids": ["anchor-synthetic"],
                "time_flags": ["clock_drift_unverified"],
                "quality": [],
                "metrics": copy.deepcopy(metrics),
            }
        )
    return night, environment


def evaluate(night, environment, **kwargs):
    return evaluate_readiness(DAY, night, environment, now=kwargs.pop("now", NOW), **kwargs)


def codes(result):
    return {row["code"] for row in result["missing"]}


def test_complete_observations_are_ready_today_despite_legacy_provisional_label():
    night, environment = sample()
    night["is_provisional"] = True
    before = copy.deepcopy((night, environment))
    result = evaluate(night, environment)
    assert result["core_ready"] and result["data_state"] == "ready"
    assert result["policy_version"] == "dual-source-coverage-2"
    assert result["policy"] == {"min_channel_coverage": 0.99, "max_affected_span_seconds": 300}
    assert result["quality_state"] == "complete" and result["warnings"] == []
    assert result["missing"] == []
    assert result["environment_summary"]["expected_windows"] == 3
    assert result["environment_summary"]["channels"]["sound_relative"]["expected_count"] == 180
    assert "clock_error_unknown" in result["limitations"]
    assert "relative_sound_uncalibrated" in result["limitations"]
    assert (night, environment) == before
    assert "source-synthetic" not in json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "key",
    [
        "sleep_seconds",
        "average_heart_rate",
        "night_average_hrv",
        "training_load",
        "previous_stress",
    ],
)
@pytest.mark.parametrize("state", ["missing", "invalid", "stale", "pending"])
def test_every_required_card_must_be_observed_and_fresh(key, state):
    night, environment = sample()
    night["cards"][key]["state"] = state
    result = evaluate(night, environment)
    assert not result["core_ready"]
    assert any(row["observed"] == state for row in result["missing"])


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), -1, 0])
def test_hrv_is_positive_finite_not_missing_or_zero(value):
    night, environment = sample()
    night["cards"]["night_average_hrv"]["value"] = value
    result = evaluate(night, environment)
    assert not result["core_ready"]
    json.dumps(result, allow_nan=False)


def test_zero_load_requires_complete_no_activity_evidence_and_wrong_source_is_rejected():
    night, environment = sample()
    del night["cards"]["training_load"]["reason"]
    night["cards"]["average_heart_rate"]["source_resource"] = "daily"
    night["cards"]["previous_stress"]["source_date"] = DAY.isoformat()
    result = evaluate(night, environment)
    assert {row["observed"] for row in result["missing"]} >= {
        "counts_incomplete",
        "source_mismatch",
        "date_mismatch",
    }


def test_optional_stale_values_and_legacy_global_quality_do_not_poison_core():
    night, environment = sample()
    night["cards"]["sleep_score"] = {"value": 50, "state": "stale"}
    night["quality"] = ["latest_request_failed", "sleep_stage_timeline_unavailable"]
    night["load_comparison"] = {"state": "stale"}
    result = evaluate(night, environment)
    assert result["core_ready"]
    assert {row["metric"] for row in result["optional_missing"]} >= {
        "sleep.score",
        "history.baseline",
        "sleep.stage_timeline",
    }


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"end_at": "2026-01-02T04:00:00Z"}, "not_closed"),
        ({"end_at": "2026-01-01T21:00:00Z"}, "invalid"),
        ({"end_at": "2026-01-03T23:00:00Z"}, "too_long"),
        ({"end_at": "2026-01-02T00:00:00"}, "missing"),
        ({"in_progress": True}, "not_closed"),
        ({"core_conflict": True}, "conflict"),
    ],
)
def test_sleep_window_must_be_closed_valid_and_at_most_24_hours(patch, reason):
    night, environment = sample()
    night["sleep"].update(patch)
    result = evaluate(night, environment)
    assert not result["core_ready"]
    assert result["garmin_summary"]["window_state"] == reason


def test_absent_sleep_does_not_invent_environment_window_or_coverage():
    result = evaluate(None, None)
    assert not result["core_ready"]
    assert result["window"] is None and result["environment_summary"] is None
    assert all(row["window_start"] is None for row in result["missing"])


def test_sleep_duration_cannot_exceed_window_and_source_day_must_match():
    night, environment = sample()
    night["cards"]["sleep_seconds"]["value"] = 181
    assert not evaluate(night, environment)["core_ready"]
    night, environment = sample()
    night["date"] = "2026-01-01"
    assert evaluate(night, environment)["garmin_summary"]["window_state"] == "date_mismatch"


def test_exact_partial_sleep_edges_keep_full_minute_counts_and_clipped_coverage():
    night, environment = sample()
    night["sleep"]["start_at"] = stamp(START + timedelta(seconds=20))
    night["sleep"]["end_at"] = stamp(START + timedelta(seconds=160))
    night["cards"]["sleep_seconds"]["value"] = 140
    result = evaluate(night, environment)
    summary = result["environment_summary"]
    assert result["core_ready"]
    assert summary["expected_windows"] == summary["observed_windows"] == 3
    assert summary["covered_seconds"] == 140 and summary["missing_seconds"] == 0
    assert summary["channels"]["temperature_c"]["expected_count"] == 18
    assert "minute_statistics_include_boundary_outside_sleep" in result["limitations"]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_missing_whole_minute_including_edge_is_counted_on_real_boot_grid(index):
    night, environment = sample()
    environment["records"].pop(index)
    result = evaluate(night, environment)
    summary = result["environment_summary"]
    assert not result["core_ready"]
    assert summary["expected_windows"] == 3 and summary["missing_windows"] == 1
    assert summary["missing_seconds"] == 60
    assert summary["channels"]["temperature_c"]["missing_count"] == 6


def test_one_missing_minute_with_known_grid_can_pass_but_gap_remains_visible():
    night, environment = sample(300)
    environment["records"].pop(180)
    result = evaluate(night, environment)
    assert result["core_ready"] and result["environment_summary"]["missing_windows"] == 1
    assert result["quality_state"] == "with_gaps"
    assert any(row["code"] == "missing_environment_minutes" for row in result["warnings"])


def test_no_windows_has_exact_missing_time_but_no_invented_boot_grid_count():
    night, environment = sample()
    environment["records"] = []
    result = evaluate(night, environment)
    summary = result["environment_summary"]
    assert not result["core_ready"]
    assert summary["expected_windows"] is None and summary["missing_windows"] is None
    assert not summary["count_exact"] and summary["missing_windows_lower_bound"] == 3
    assert summary["missing_seconds"] == 180


def test_identical_duplicate_deduplicates_but_conflicting_duplicate_blocks():
    night, environment = sample()
    environment["records"].append(copy.deepcopy(environment["records"][0]))
    result = evaluate(night, environment)
    assert result["core_ready"] and result["environment_summary"]["observed_windows"] == 3
    environment["records"][-1]["metrics"]["temperature_c"]["mean"] = 99
    assert "time_conflict" in codes(evaluate(night, environment))


def test_same_profile_multiple_boots_can_cover_without_gap_or_overlap():
    night, environment = sample()
    environment["records"][2]["boot_ref"] = "boot-next"
    assert evaluate(night, environment)["core_ready"]


def test_cross_boot_gap_does_not_invent_switch_time_or_exact_expected_count():
    night, environment = sample()
    environment["records"][2]["boot_ref"] = "boot-next"
    environment["records"].pop(1)
    result = evaluate(night, environment)
    summary = result["environment_summary"]
    assert not result["core_ready"]
    assert summary["missing_seconds"] == 60
    assert summary["expected_windows"] is None and not summary["count_exact"]
    assert summary["missing_windows_lower_bound"] == 1


def test_overlap_and_same_boot_grid_shift_never_count_as_complete():
    night, environment = sample()
    extra = copy.deepcopy(environment["records"][0])
    extra.update(record_ref="extra-record", boot_ref="other-boot")
    environment["records"].append(extra)
    result = evaluate(night, environment)
    assert not result["core_ready"] and result["environment_summary"]["overlap_seconds"] == 60
    night, environment = sample()
    row = environment["records"][1]
    row["utc_start"] = stamp(START + timedelta(seconds=70))
    row["utc_end"] = stamp(START + timedelta(seconds=130))
    assert any(
        row["observed"] == "grid_mismatch" for row in evaluate(night, environment)["missing"]
    )


def test_changed_profile_or_missing_placement_blocks_even_when_coverage_full():
    night, environment = sample()
    second = {**environment["sources"][0], "source_ref": "source-next"}
    environment["sources"].append(second)
    environment["records"][2]["source_ref"] = "source-next"
    result = evaluate(night, environment)
    assert "configuration_changed" in codes(result)
    assert "invalid_channel" not in codes(result)
    assert all(value is None for value in result["environment_summary"]["channels"].values())
    night, environment = sample()
    del environment["sources"][0]["placement_ref"]
    assert "configuration_changed" in codes(evaluate(night, environment))


def test_explicit_source_selection_excludes_unselected_overlapping_source():
    night, environment = sample()
    extra = copy.deepcopy(environment["records"][0])
    extra.update(record_ref="unselected", source_ref="source-other", boot_ref="boot-other")
    environment["records"].append(extra)
    environment["selected_source"] = "source-synthetic"
    assert evaluate(night, environment)["core_ready"]


@pytest.mark.parametrize(
    "key,value", [("time_quality", "unknown"), ("time_quality", "conflict"), ("anchor_ids", [])]
)
def test_time_mapping_must_belong_to_this_boot_and_have_usable_anchor(key, value):
    night, environment = sample()
    environment["records"][1][key] = value
    result = evaluate(night, environment)
    assert not result["core_ready"]
    assert codes(result) & {"unknown_environment_time", "time_conflict"}


def test_unrelated_historical_unknown_boot_does_not_poison_complete_night():
    night, environment = sample()
    environment["global_time_state"].update(unknown_record_count=999, conflict_record_count=100)
    assert evaluate(night, environment)["core_ready"]
    environment["records"] = []
    result = evaluate(night, environment)
    assert "unknown_environment_time_possible" in result["limitations"]
    assert "unknown_environment_time" in codes(result)


@pytest.mark.parametrize(
    "metric", ["temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative"]
)
def test_each_channel_uses_own_denominator_and_sound_requires_consistent_seconds(metric):
    night, environment = sample()
    channel = environment["records"][1]["metrics"][metric]
    channel["valid_count"] -= 1
    result = evaluate(night, environment)
    assert result["core_ready"] is (metric == "illuminance_lux")
    assert result["environment_summary"]["channels"][metric]["valid_windows"] == 2
    assert result["environment_summary"]["channels"][metric]["missing_count"] == 1
    assert any(
        row["metric"] == metric and row["code"] == "invalid_channel"
        for row in result["missing"] + result["warnings"]
    )


def test_forged_smaller_expected_count_does_not_hide_missing_samples():
    night, environment = sample()
    channel = environment["records"][0]["metrics"]["temperature_c"]
    channel.update(expected_count=5, observed_count=5, valid_count=5)
    result = evaluate(night, environment)
    assert not result["core_ready"]
    assert result["environment_summary"]["channels"]["temperature_c"]["expected_count"] == 18


@pytest.mark.parametrize(
    "patch",
    [
        {"valid_samples": 959999},
        {"observed_samples": 959999},
        {"expected_samples": 959999},
        {"valid_1s_count": 59},
        {"valid_duration_ms": 59000},
        {"dma_overflow_count": 1},
        {"clipped_samples": 1},
        {"energy_mean_dbfs": None},
        {"energy_mean_dbfs": float("nan")},
        {"quality": ["audio_gap"]},
        {"quality": ["clipped"]},
    ],
)
def test_sound_requires_full_pcm_seconds_no_overflow_or_clipping(patch):
    night, environment = sample()
    environment["records"][1]["metrics"]["sound_relative"].update(patch)
    result = evaluate(night, environment)
    assert not result["core_ready"]
    assert any(row["metric"] == "sound_relative" for row in result["missing"])
    json.dumps(result, allow_nan=False)


def test_event_warmup_is_optional_and_does_not_reject_complete_sound_channel():
    night, environment = sample()
    environment["records"][0]["metrics"]["sound_relative"].update(
        event_detector_state="warming", event_valid_duration_ms=0, quality=["warming"]
    )
    environment["records"][0]["metrics"]["illuminance_lux"]["quality"] = ["low_light_limit"]
    result = evaluate(night, environment)
    assert result["core_ready"]
    assert "event_detection_incomplete" in result["limitations"]
    assert any(row["metric"] == "environment.sound_events" for row in result["optional_missing"])


@pytest.mark.parametrize(
    "patch", [{"truncated": True}, {"state": "not_initialized"}, {"state": "unavailable"}]
)
def test_unavailable_or_truncated_environment_is_never_ready(patch):
    night, environment = sample()
    environment.update(patch)
    assert not evaluate(night, environment)["core_ready"]


def test_expiry_is_separate_from_actual_completeness_and_uses_shanghai_date():
    night, environment = sample()
    assert evaluate(night, environment, now=NOW + timedelta(days=6))["data_state"] == "ready"
    result = evaluate(night, environment, now=NOW + timedelta(days=7))
    assert result["data_state"] == "expired_unanalyzed" and result["core_ready"]
    environment["records"].pop()
    result = evaluate(night, environment, now=NOW + timedelta(days=7))
    assert result["data_state"] == "expired_unanalyzed" and not result["core_ready"]
    assert (
        evaluate(night, environment, now=datetime(2026, 1, 8, 16, tzinfo=UTC))["data_state"]
        == "expired_unanalyzed"
    )


def test_every_gap_has_complete_contract_and_safe_fixed_observations():
    night, environment = sample()
    night["cards"]["sleep_seconds"]["state"] = "private untrusted error"
    environment["records"][1]["metrics"]["temperature_c"]["quality"] = ["bus_error"]
    result = evaluate(night, environment)
    for item in result["missing"] + result["optional_missing"]:
        assert set(item) == {
            "code",
            "source",
            "metric",
            "date",
            "window_start",
            "window_end",
            "expected",
            "observed",
            "recoverability",
            "last_checked_at",
        }
        assert item["recoverability"] in {"await_sync", "await_upload", "unrecoverable", "unknown"}
    assert "private untrusted error" not in json.dumps(result)


@pytest.mark.parametrize("days", [0, 32, True, 2.5])
def test_invalid_policy_configuration_fails_explicitly(days):
    with pytest.raises(ValueError, match="lookback"):
        evaluate_readiness(DAY, None, None, now=NOW, lookback_days=days)


def test_naive_cutoff_is_rejected():
    with pytest.raises(ValueError, match="aware_now"):
        evaluate_readiness(DAY, None, None, now=NOW.replace(tzinfo=None))


def lose_samples(environment, index, metric, count):
    """A synthetic wire-valid partial summary; only complete seconds enter sound energy."""
    value = environment["records"][index]["metrics"][metric]
    value["valid_count"] -= count
    value["state"] = "partial" if value["valid_count"] else "missing"
    value["quality"] = ["sensor_error", "partial_window"]
    if metric == "sound_relative":
        value["valid_1s_count"] = value["valid_count"]
        value["valid_duration_ms"] = value["valid_count"] * 1000
        value["quality"].append("audio_gap")
        # Incomplete seconds may still contain PCM; these samples are not usable seconds.
        assert value["valid_samples"] >= value["valid_count"] * 16000
        if not value["valid_count"]:
            value["energy_mean_dbfs"] = value["max_1s_rms_dbfs"] = None
    elif not value["valid_count"]:
        value["min"] = value["mean"] = value["max"] = None
    return value


def light_bus_gap(environment, index, count=2):
    value = lose_samples(environment, index, "illuminance_lux", count)
    value["quality"] += ["bus_error", "warming", "low_light_limit"]
    return value


@pytest.mark.parametrize("missing_samples", [1, 2])
def test_counted_light_bus_gap_retains_valid_samples_and_downgrades_to_warning(missing_samples):
    night, environment = sample(10)
    light_bus_gap(environment, 2, missing_samples)
    before = copy.deepcopy(environment)
    result = evaluate(night, environment)
    channel = result["environment_summary"]["channels"]["illuminance_lux"]
    assert result["core_ready"] and result["quality_state"] == "with_gaps"
    assert channel["usable_count"] == channel["valid_count"] == 600 - missing_samples
    assert channel["missing_count"] == missing_samples
    assert channel["effective_coverage_ratio"] == (600 - missing_samples) / 600
    assert channel["valid_windows"] == 9 and channel["max_affected_span_seconds"] == 60
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert warning["code"] == "light_bus_sampling_gap"
    assert warning["window_start"] == stamp(START + timedelta(minutes=2))
    assert warning["window_end"] == stamp(START + timedelta(minutes=3))
    assert (warning["expected"], warning["observed"]) == (60, 60 - missing_samples)
    assert environment == before


@pytest.mark.parametrize(
    ("count", "patch"),
    [
        (0, {}),
        (3, {}),
        (60, {}),
        (2, {"observed_count": 59}),
        (2, {"unit": "percent"}),
        (2, {"mean": None}),
        (2, {"quality": ["bus_error", "warming"]}),
        (2, {"quality": ["bus_error", "sensor_error", "out_of_range"]}),
    ],
)
def test_light_bus_exception_excludes_contradictions_larger_gaps_and_other_errors(count, patch):
    night, environment = sample(110)
    light_bus_gap(environment, 2, count).update(patch)
    result = evaluate(night, environment)
    assert not result["core_ready"]
    assert codes(result) == {"invalid_channel"}


@pytest.mark.parametrize(
    ("minutes", "affected", "blocker"),
    [(3, 1, "insufficient_channel_coverage"), (30, 6, "excessive_sampling_gap")],
)
def test_counted_light_bus_gap_still_obeys_both_night_thresholds(minutes, affected, blocker):
    night, environment = sample(minutes)
    for index in range(affected):
        light_bus_gap(environment, index)
    result = evaluate(night, environment)
    assert result["data_state"] == "waiting_data"
    assert blocker in codes(result)
    assert not result["warnings"]


def test_partial_sound_uses_complete_seconds_not_pcm_count_and_retains_true_gaps():
    night, environment = sample(100)
    for index, lost in ((10, 2), (30, 3), (70, 2)):
        lose_samples(environment, index, "sound_relative", lost)
    before = copy.deepcopy(environment)
    result = evaluate(night, environment)
    channel = result["environment_summary"]["channels"]["sound_relative"]
    assert result["core_ready"] and result["quality_state"] == "with_gaps"
    assert result["missing"] == []
    assert channel["usable_count"] == channel["valid_count"] == 5993
    assert channel["effective_coverage_ratio"] == pytest.approx(5993 / 6000)
    assert channel["effective_coverage_ratio"] < 1
    assert channel["valid_windows"] == 97 and channel["coverage_ratio"] == 0.97
    assert channel["max_affected_span_seconds"] == 60
    assert channel["audio_missing_seconds"] == 7
    assert len([row for row in result["warnings"] if row["metric"] == "sound_relative"]) == 3
    assert environment == before


@pytest.mark.parametrize(
    "metric", ["temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative"]
)
def test_99_percent_is_inclusive_but_one_additional_sample_blocks(metric):
    night, environment = sample(100)
    environment["records"].pop(20)
    result = evaluate(night, environment)
    channel = result["environment_summary"]["channels"][metric]
    assert result["core_ready"] and result["quality_state"] == "with_gaps"
    assert channel["effective_coverage_ratio"] == 0.99
    assert result["environment_summary"]["missing_windows"] == 1
    lose_samples(environment, 40, metric, 1)
    result = evaluate(night, environment)
    assert not result["core_ready"] and result["quality_state"] == "insufficient"
    assert any(
        row["code"] == "insufficient_channel_coverage" and row["metric"] == metric
        for row in result["missing"]
    )


@pytest.mark.parametrize("lost_minutes,eligible", [(5, True), (6, False)])
def test_long_gap_cannot_hide_in_an_adequate_nightly_average(lost_minutes, eligible):
    night, environment = sample(600)
    del environment["records"][100 : 100 + lost_minutes]
    result = evaluate(night, environment, now=START + timedelta(days=1))
    assert result["core_ready"] is eligible
    for channel in result["environment_summary"]["channels"].values():
        assert channel["effective_coverage_ratio"] >= 0.99
        assert channel["max_affected_span_seconds"] == lost_minutes * 60
    assert ("excessive_sampling_gap" in codes(result)) is (not eligible)


def test_consecutive_partial_minutes_are_affected_ranges_not_precise_lost_seconds():
    night, environment = sample(100)
    for index in range(10, 16):
        lose_samples(environment, index, "sound_relative", 1)
    result = evaluate(night, environment)
    channel = result["environment_summary"]["channels"]["sound_relative"]
    assert not result["core_ready"]
    assert channel["audio_missing_seconds"] == 6
    assert channel["effective_coverage_ratio"] == 0.999
    assert channel["max_affected_span_seconds"] == 360
    assert "excessive_sampling_gap" in codes(result)


def test_missing_and_partial_adjacent_windows_share_one_affected_span():
    night, environment = sample(600)
    for index in (10, 11, 14, 15):
        lose_samples(environment, index, "sound_relative", 1)
    del environment["records"][12:14]
    result = evaluate(night, environment, now=START + timedelta(days=1))
    assert (
        result["environment_summary"]["channels"]["sound_relative"]["max_affected_span_seconds"]
        == 360
    )
    assert not result["core_ready"]


def test_affected_span_clips_at_sleep_edges_without_reweighting_sample_counts():
    night, environment = sample(100)
    for index in range(6):
        lose_samples(environment, index, "sound_relative", 1)
    night["sleep"]["start_at"] = stamp(START + timedelta(seconds=60))
    night["cards"]["sleep_seconds"]["value"] -= 60
    result = evaluate(night, environment)
    channel = result["environment_summary"]["channels"]["sound_relative"]
    assert result["core_ready"]
    assert channel["max_affected_span_seconds"] == 300
    assert channel["expected_count"] == 99 * 60
    assert channel["usable_count"] == 99 * 60 - 5


@pytest.mark.parametrize(
    "metric", ["temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative"]
)
def test_zero_valid_samples_with_explicit_null_statistics_is_a_real_sampling_gap(metric):
    night, environment = sample(100)
    value = environment["records"][10]["metrics"][metric]
    lose_samples(environment, 10, metric, value["expected_count"])
    result = evaluate(night, environment)
    channel = result["environment_summary"]["channels"][metric]
    assert result["core_ready"] and result["quality_state"] == "with_gaps"
    assert channel["effective_coverage_ratio"] == 0.99
    assert any(row["metric"] == metric for row in result["warnings"])


@pytest.mark.parametrize("metric", ["temperature_c", "relative_humidity_pct", "illuminance_lux"])
def test_legitimate_zero_scalar_values_do_not_count_as_missing(metric):
    night, environment = sample()
    value = environment["records"][1]["metrics"][metric]
    value.update(min=0, mean=0, max=0)
    result = evaluate(night, environment)
    assert result["core_ready"] and result["quality_state"] == "complete"


@pytest.mark.parametrize(
    "quality", ["bus_error", "crc_error", "out_of_range", "dma_overflow", "clipped"]
)
def test_small_severe_channel_errors_remain_blocking_under_99_percent(quality):
    night, environment = sample(600)
    value = lose_samples(environment, 10, "sound_relative", 1)
    value["quality"].append(quality)
    result = evaluate(night, environment, now=START + timedelta(days=1))
    assert not result["core_ready"]
    assert (
        result["environment_summary"]["channels"]["sound_relative"]["effective_coverage_ratio"]
        > 0.99
    )
    assert any(
        row["code"] == "invalid_channel" and row["observed"] == "invalid"
        for row in result["missing"]
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"unit": "dBA"},
        {"expected_count": 59},
        {"observed_count": 58},
        {"valid_count": -1},
        {"valid_count": True},
        {"valid_1s_count": 58},
        {"valid_duration_ms": 59001},
        {"expected_samples": 959999},
        {"valid_samples": 1},
        {"observed_samples": 960001},
        {"valid_samples": 960001},
        {"energy_mean_dbfs": None},
        {"energy_mean_dbfs": 1},
        {"max_1s_rms_dbfs": -100},
        {"dma_overflow_count": 1},
        {"clipped_samples": 1},
    ],
)
def test_partial_sound_structural_errors_cannot_be_relabelled_usable(patch):
    night, environment = sample(600)
    value = lose_samples(environment, 10, "sound_relative", 1)
    value.update(patch)
    result = evaluate(night, environment, now=START + timedelta(days=1))
    assert not result["core_ready"]
    assert any(
        row["code"] == "invalid_channel" and row["observed"] == "invalid"
        for row in result["missing"]
    )
    json.dumps(result, allow_nan=False)


def test_absent_statistic_field_is_not_equivalent_to_an_explicit_null_observation():
    night, environment = sample(100)
    value = lose_samples(environment, 10, "temperature_c", 6)
    del value["mean"]
    assert not evaluate(night, environment)["core_ready"]


def test_unknown_time_and_unknown_cross_boot_denominator_still_block_small_gaps():
    night, environment = sample(100)
    environment["records"][10]["time_quality"] = "unknown"
    result = evaluate(night, environment)
    assert not result["core_ready"] and "unknown_environment_time" in codes(result)
    night, environment = sample(100)
    environment["records"][-1]["boot_ref"] = "new-boot"
    environment["records"].pop(10)
    result = evaluate(night, environment)
    assert not result["core_ready"]
    assert all(
        channel["effective_coverage_ratio"] is None
        for channel in result["environment_summary"]["channels"].values()
    )


def test_expired_and_optional_status_are_separate_from_tolerated_core_quality():
    night, environment = sample(100)
    lose_samples(environment, 10, "sound_relative", 2)
    result = evaluate(night, environment, now=NOW + timedelta(days=7))
    assert result["core_ready"] and result["quality_state"] == "with_gaps"
    assert result["data_state"] == "expired_unanalyzed"
    assert result["missing"] == [] and result["optional_missing"]
    assert all(row["source"] == "environment" for row in result["warnings"])


def test_warning_compression_preserves_valid_minute_between_partial_minutes():
    night, environment = sample(100)
    for index in (10, 11, 13):
        lose_samples(environment, index, "sound_relative", 1)
    result = evaluate(night, environment)
    warnings = [row for row in result["warnings"] if row["metric"] == "sound_relative"]
    assert result["core_ready"] and len(warnings) == 2
    assert warnings[0]["window_end"] != warnings[1]["window_start"]


def test_optional_availability_uses_only_known_boolean_proofs_and_unsupported_is_distinct():
    night, environment = sample()
    night["sleep"]["stage_timeline_available"] = False
    night["optional_availability"] = {
        "sleep.spo2": True,
        "sleep.body_battery": True,
        "previous.activities": True,
        "history.baseline": "true",
        "lifestyle.context": True,
    }
    result = evaluate(night, environment)
    missing = {row["metric"]: row for row in result["optional_missing"]}
    assert "sleep.spo2" not in missing and "sleep.body_battery" not in missing
    assert "previous.activities" not in missing
    assert "history.baseline" in missing and "lifestyle.context" in missing
    assert missing["sleep.stage_timeline"]["observed"] == "unsupported"
    assert result["core_ready"]


def test_continuous_all_channel_failures_are_compressed_without_losing_coverage():
    night, environment = sample(1440)
    for row in environment["records"]:
        for channel in row["metrics"].values():
            channel["quality"] = ["sensor_error"]
    result = evaluate(night, environment, now=START + timedelta(days=2))
    missing = [row for row in result["missing"] if row["code"] == "invalid_channel"]
    assert len(missing) == 4
    assert all(row["window_start"] == stamp(START) for row in missing)
    assert all(row["window_end"] == stamp(START + timedelta(days=1)) for row in missing)
    assert all(
        channel["invalid_windows"] == 1440
        for channel in result["environment_summary"]["channels"].values()
    )
    assert all(
        channel["coverage_ratio"] == 0
        for channel in result["environment_summary"]["channels"].values()
    )


def test_compression_does_not_bridge_a_valid_minute_or_change_reason():
    night, environment = sample()
    environment["records"][0]["metrics"]["temperature_c"]["quality"] = ["bus_error"]
    environment["records"][2]["metrics"]["temperature_c"]["quality"] = ["bus_error"]
    missing = [
        row
        for row in evaluate(night, environment)["missing"]
        if row["metric"] == "temperature_c" and row["code"] == "invalid_channel"
    ]
    assert len(missing) == 2
    assert missing[0]["window_end"] != missing[1]["window_start"]


def test_record_quality_failure_is_not_ignored_even_with_valid_channel_counts():
    night, environment = sample()
    environment["records"][1]["quality"] = ["partial_window"]
    assert not evaluate(night, environment)["core_ready"]


def test_previous_day_gaps_do_not_inherit_cross_midnight_sleep_window():
    night, environment = sample()
    start = datetime(2026, 1, 1, 15, 59, tzinfo=UTC)
    night["sleep"].update(start_at=stamp(start), end_at=stamp(start + timedelta(minutes=3)))
    for offset, row in enumerate(environment["records"]):
        row["utc_start"] = stamp(start + timedelta(minutes=offset))
        row["utc_end"] = stamp(start + timedelta(minutes=offset + 1))
    for name in ("training_load", "previous_stress", "night_average_hrv"):
        night["cards"][name]["state"] = "missing"
    night["optional_availability"] = {"previous.activities": False}
    result = evaluate(night, environment)
    findings = {item["metric"]: item for item in result["missing"] + result["optional_missing"]}
    for metric in ("previous.training_load", "previous.average_stress", "previous.activities"):
        assert findings[metric]["date"] == "2026-01-01"
        assert findings[metric]["window_start"] is None
        assert findings[metric]["window_end"] is None
    sleep_gap = findings["sleep.night_average_hrv"]
    assert sleep_gap["date"] == DAY.isoformat()
    assert sleep_gap["window_start"] == night["sleep"]["start_at"]
    assert sleep_gap["window_end"] == night["sleep"]["end_at"]


@pytest.mark.parametrize("flag", ["sensor_error", "audio_gap", "dma_overflow", "clipped"])
def test_sound_channel_quality_does_not_invalidate_other_three_channels(flag):
    night, environment = sample()
    environment["records"][1]["metrics"]["sound_relative"]["quality"] = [flag]
    result = evaluate(night, environment)
    assert not result["core_ready"]
    invalid = {row["metric"] for row in result["missing"] if row["code"] == "invalid_channel"}
    assert invalid == {"sound_relative"}
    channels = result["environment_summary"]["channels"]
    assert channels["sound_relative"]["invalid_windows"] == 1
    for metric in ("temperature_c", "relative_humidity_pct", "illuminance_lux"):
        assert channels[metric]["invalid_windows"] == 0
        assert channels[metric]["valid_windows"] == 3
        assert channels[metric]["coverage_ratio"] == 1


def test_bad_phase_does_not_remove_later_windows_back_on_original_grid():
    night, environment = sample()
    row = environment["records"][1]
    row["utc_start"] = stamp(START + timedelta(seconds=70))
    row["utc_end"] = stamp(START + timedelta(seconds=130))
    result = evaluate(night, environment)
    assert not result["core_ready"]
    assert result["environment_summary"]["observed_windows"] == 2


def test_sleep_start_and_end_do_not_have_to_match_clock_minute_grid():
    night, environment = sample()
    for row in environment["records"]:
        for field in ("utc_start", "utc_end"):
            row[field] = stamp(datetime.fromisoformat(row[field]) + timedelta(seconds=9.226))
    night["sleep"]["start_at"] = stamp(START + timedelta(seconds=9.226))
    night["sleep"]["end_at"] = stamp(START + timedelta(seconds=189.226))
    assert evaluate(night, environment)["core_ready"]


def test_daily_heart_rate_or_weekly_hrv_field_cannot_replace_sleep_specific_value():
    night, environment = sample()
    night["cards"]["night_average_hrv"]["source_field"] = "weeklyAvg"
    night["cards"]["average_heart_rate"]["source_field"] = "restingHeartRate"
    result = evaluate(night, environment)
    assert not result["core_ready"]
    assert len([row for row in result["missing"] if row["observed"] == "source_mismatch"]) == 2
