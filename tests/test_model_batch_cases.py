"""Offline verification of the synthetic corpus used by explicit live model tests."""

from collections import Counter

import pytest
from model_batch_cases import SCENARIOS, generate_cases

from sleepscape.domain.sleep_features import (
    CORE_METRICS,
    MAX_INPUT_BYTES,
    canonical_json,
    input_hash,
)
from sleepscape.resources import validate_sleep_input


@pytest.fixture(scope="module")
def cases():
    return generate_cases()


def metric(payload, name):
    return next(item for item in payload["facts"] if item["metric"] == name)


def test_fifty_distinct_synthetic_inputs_are_valid_bounded_and_anonymous(cases):
    assert len(cases) == len({row["case_id"] for row in cases}) == 50
    assert len({row["payload"]["input_hash"] for row in cases}) == 50
    assert Counter(row["scenario"] for row in cases) == dict.fromkeys(SCENARIOS, 5)
    for row in cases:
        payload = row["payload"]
        validate_sleep_input(payload)
        assert payload["synthetic"] is True and payload["readiness"] == "ready"
        assert payload["input_hash"] == input_hash(payload)
        assert len(canonical_json(payload).encode()) <= MAX_INPUT_BYTES
        assert all(metric(payload, name)["quality"] == "valid" for name in CORE_METRICS)
        raw = canonical_json(payload)
        for forbidden in (
            "device-synthetic",
            "source-synthetic",
            "placement-synthetic",
            "boot_ref",
        ):
            assert forbidden not in raw


def test_generation_is_reproducible_and_changes_measurements_with_seed(cases):
    assert generate_cases(count=3) == cases[:3]
    other = generate_cases(seed=20261119, count=3)
    assert [row["case_id"] for row in other] == [row["case_id"] for row in cases[:3]]
    assert all(
        left["payload"]["input_hash"] != right["payload"]["input_hash"]
        for left, right in zip(other, cases, strict=False)
    )
    assert any(
        metric(left["payload"], "sleep.average_heart_rate")["value"]
        != metric(right["payload"], "sleep.average_heart_rate")["value"]
        for left, right in zip(other, cases, strict=False)
    )


def test_coverage_scenarios_represent_real_counts_and_boundary_limits(cases):
    assert Counter(row["payload"]["completeness"]["quality_state"] for row in cases) == {
        "complete": 25,
        "with_gaps": 25,
    }
    for row in cases:
        payload = row["payload"]
        channels = payload["completeness"]["channels"]
        if row["scenario"] == "exact_99_percent_missing_minutes":
            assert payload["completeness"]["environment_expected_windows"] == 300
            assert payload["completeness"]["environment_observed_windows"] == 297
            assert all(c["effective_coverage_ratio"] == 0.99 for c in channels.values())
            assert all(c["max_affected_span_seconds"] == 180 for c in channels.values())
        if row["scenario"] == "five_affected_minutes_sound_gap":
            sound = channels["sound_relative"]
            assert sound["expected_count"] - sound["usable_count"] == 5
            assert sound["max_affected_span_seconds"] == 300
        if payload["completeness"]["quality_state"] == "with_gaps":
            assert payload["completeness"]["warnings"]
            assert "sampling_gaps_present" in payload["required_limitations"]


def test_background_context_includes_independent_baselines_and_both_directions(cases):
    denominators = set()
    load_changes = []
    for row in cases:
        payload = row["payload"]
        denominators.add(metric(payload, "baseline.average_heart_rate")["valid_n"])
        if row["scenario"] == "stress_light_events_independent_baselines":
            assert metric(payload, "baseline.average_heart_rate")["valid_n"] == 7
            assert metric(payload, "baseline.average_heart_rate")["quality"] == "valid"
            assert metric(payload, "baseline.night_average_hrv")["valid_n"] == 6
            assert metric(payload, "baseline.night_average_hrv")["quality"] == "unavailable"
        if row["scenario"] == "rest_dark_complete_no_baseline":
            assert metric(payload, "previous.training_load")["value"] == 0
            assert "baseline_insufficient" in payload["required_limitations"]
        for item in payload["facts"]:
            if item["metric"] == "comparison.training_load_percent":
                load_changes.append(item["value"])
    assert denominators == {0, 5, 7, 14, 30}
    assert min(load_changes) < 0 < max(load_changes)


def test_events_timeline_omission_and_clock_quality_are_explicit(cases):
    payloads = [row["payload"] for row in cases]
    assert {p["event_status"] for p in payloads} >= {
        "available",
        "none_detected",
        "unavailable",
        "context_truncated",
    }
    assert {p["completeness"]["time_quality"] for p in payloads} == {"anchored", "estimated"}
    assert any(p["timeline"] for p in payloads)
    assert any(not p["timeline"] for p in payloads)
    for row in cases:
        payload = row["payload"]
        if row["scenario"] == "sound_events_truncated_full_baseline":
            assert len(payload["events"]) == 8
            assert payload["event_status"] == "context_truncated"
            assert "event_context_truncated" in payload["required_limitations"]
        if not payload["timeline"]:
            assert "timeline_omitted_for_budget" in payload["required_limitations"]


@pytest.mark.parametrize("count", [0, 51, -1, True, 1.5])
def test_corpus_count_is_bounded(count):
    with pytest.raises(ValueError, match="invalid_synthetic_case_count"):
        generate_cases(count=count)


@pytest.mark.parametrize("seed", [-1, 2**32, True, 2.5])
def test_seed_is_explicit_and_reproducible(seed):
    with pytest.raises(ValueError, match="invalid_synthetic_seed"):
        generate_cases(seed=seed)
