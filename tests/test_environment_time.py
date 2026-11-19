"""Synthetic boot-local time mapping, with no IO or clock accuracy assumptions."""

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from itertools import permutations
from random import Random

import pytest

from sleepscape.domain.environment import MAX_SAFE_INTEGER, TimeAnchor
from sleepscape.domain.environment_time import (
    ANCHOR_CONFLICT_TOLERANCE_MS,
    TIME_MAPPING_ALGORITHM,
    BootTimeMapper,
    build_time_mapper,
    summarize_intervals,
    us_to_utc,
    utc_to_us,
)


def anchor(
    name="a",
    mono=60_000,
    utc="2026-01-02T00:01:00Z",
    *,
    source="ntp",
    uncertainty=None,
    quality=(),
):
    return TimeAnchor(
        anchor_id=name,
        monotonic_ms=mono,
        utc_at=utc,
        source=source,
        uncertainty_ms=uncertainty,
        quality=list(quality),
    )


def test_unanchored_boot_has_no_utc_and_does_not_borrow_from_previous_boot():
    previous_boot = build_time_mapper([anchor()])
    current_boot = build_time_mapper([])
    assert previous_boot.map_interval(0, 60_000).utc_start == "2026-01-02T00:00:00Z"
    result = current_boot.map_interval(0, 60_000)
    assert result.time_quality == "unknown"
    assert result.utc_start is result.utc_end is result.time_uncertainty_ms is None
    assert result.anchor_ids == ()
    assert "no_usable_time_anchor" in result.quality


@pytest.mark.parametrize("source,uncertainty", [("host_usb", None), ("ntp", None), ("ntp", 0.0)])
def test_current_unverified_drift_never_claims_synced_or_known_accuracy(source, uncertainty):
    mapper = build_time_mapper([anchor(source=source, uncertainty=uncertainty)])
    nearby = mapper.map_interval(60_000, 120_000)
    far = mapper.map_interval(48 * 60 * 60 * 1000, 48 * 60 * 60 * 1000 + 60_000)
    assert nearby.time_quality == far.time_quality == "estimated"
    assert nearby.time_uncertainty_ms is far.time_uncertainty_ms is None
    assert nearby.mapping_algorithm == TIME_MAPPING_ALGORITHM
    assert "clock_drift_unverified" in nearby.quality


def test_mapping_preserves_submillisecond_anchor_and_half_open_adjacency():
    mapper = build_time_mapper([anchor(utc="2026-01-02T00:01:00.000123Z")])
    first = mapper.map_interval(0, 60_000)
    second = mapper.map_interval(60_000, 120_000)
    assert first.utc_start == "2026-01-02T00:00:00.000123Z"
    assert first.utc_end == second.utc_start == "2026-01-02T00:01:00.000123Z"
    assert second.utc_end == "2026-01-02T00:02:00.000123Z"


def test_consistent_anchor_order_and_nearest_anchor_do_not_move_windows():
    anchors = [
        anchor("z", 60_000),
        anchor("a", 0, "2026-01-02T00:00:00.250Z"),
        anchor("b", 120_000, "2026-01-02T00:02:01Z"),
    ]
    expected = build_time_mapper(anchors)
    for ordered in permutations(anchors):
        mapper = build_time_mapper(ordered)
        assert mapper == expected
        first = mapper.map_interval(0, 60_000)
        second = mapper.map_interval(60_000, 120_000)
        assert first.anchor_ids == second.anchor_ids == ("a",)
        assert first.utc_end == second.utc_start
        assert first.utc_start == "2026-01-02T00:00:00.250000Z"


def test_late_anchor_rebuild_changes_derived_mapping_without_mutating_evidence():
    observations = [anchor("later", 120_000, "2026-01-02T00:02:01Z")]
    evidence_before = observations[0].model_dump_json()
    old = build_time_mapper(observations)
    new = build_time_mapper([*observations, anchor("early", 0, "2026-01-02T00:00:00Z")])
    assert old.map_interval(0, 60_000).utc_start == "2026-01-02T00:00:01Z"
    assert new.map_interval(0, 60_000).utc_start == "2026-01-02T00:00:00Z"
    assert observations[0].model_dump_json() == evidence_before
    observations[0].utc_at = "2026-01-03T00:02:01Z"
    assert old.map_interval(0, 60_000).utc_start == "2026-01-02T00:00:01Z"
    with pytest.raises(FrozenInstanceError):
        old.time_quality = "synced"


@pytest.mark.parametrize("offset_ms,conflict", [(2000, False), (2001, True), (-2001, True)])
def test_clock_jump_tolerance_is_only_a_consistency_rule(offset_ms, conflict):
    changed = datetime(2026, 1, 2, 0, 2, tzinfo=UTC) + timedelta(milliseconds=offset_ms)
    mapper = build_time_mapper(
        [anchor(), anchor("b", 120_000, changed.isoformat().replace("+00:00", "Z"))]
    )
    result = mapper.map_interval(0, 60_000)
    assert ANCHOR_CONFLICT_TOLERANCE_MS == 2000
    assert result.time_quality == ("conflict" if conflict else "estimated")
    assert result.time_uncertainty_ms is None
    if conflict:
        assert result.utc_start is result.utc_end is None
        assert "whole_boot_conflict" in result.quality
        # The conservative rejection also applies outside the anchor interval.
        assert mapper.map_interval(240_000, 300_000).time_quality == "conflict"


def test_declared_error_envelopes_are_considered_but_do_not_claim_total_accuracy():
    within = [
        anchor(uncertainty=500.0),
        anchor("b", 120_000, "2026-01-02T00:02:03Z", uncertainty=500.0),
    ]
    assert build_time_mapper(within).map_interval(0, 60_000).time_quality == "estimated"
    beyond = [within[0], anchor("b", 120_000, "2026-01-02T00:02:03.001Z", uncertainty=500.0)]
    assert build_time_mapper(beyond).map_interval(0, 60_000).time_quality == "conflict"


def test_imprecise_reference_does_not_hide_conflicting_precise_anchors():
    anchors = [
        anchor("reference", 0, "2026-01-02T00:00:00Z", uncertainty=60_000.0),
        anchor("negative", 60_000, "2026-01-02T00:00:58Z", uncertainty=0.0),
        anchor("positive", 120_000, "2026-01-02T00:02:02Z", uncertainty=0.0),
    ]
    expected = build_time_mapper(anchors)
    for ordered in permutations(anchors):
        assert build_time_mapper(ordered) == expected
    result = expected.map_interval(0, 60_000)
    assert result.time_quality == "conflict"
    assert result.anchor_ids == ("reference", "negative", "positive")


def test_same_monotonic_time_disagreement_is_not_hidden_by_anchor_sorting():
    anchors = [anchor("b", 0, "2026-01-02T00:00:04Z"), anchor("a", 0, "2026-01-02T00:00:00Z")]
    assert build_time_mapper(anchors) == build_time_mapper(list(reversed(anchors)))
    assert build_time_mapper(anchors).map_interval(0, 60_000).time_quality == "conflict"


@pytest.mark.parametrize("flag", ["time_unknown", "missing", "incomplete", "sensor_error"])
def test_flagged_anchors_cannot_establish_time(flag):
    invalid = anchor("invalid", quality=[flag])
    result = build_time_mapper([invalid]).map_interval(0, 60_000)
    assert result.time_quality == "unknown"
    assert result.utc_start is None
    assert "anchor_quality_rejected" in result.quality
    usable = build_time_mapper([invalid, anchor("valid")]).map_interval(0, 60_000)
    assert usable.time_quality == "estimated"
    assert usable.anchor_ids == ("valid",)
    assert "anchor_quality_rejected" in usable.quality


def test_late_first_anchor_resolves_unknown_boot_but_late_jump_blocks_rebuild():
    assert build_time_mapper([]).map_interval(0, 60_000).time_quality == "unknown"
    first = anchor("first", 120_000, "2026-01-02T00:02:00Z")
    assert build_time_mapper([first]).map_interval(0, 60_000).time_quality == "estimated"
    late = anchor("jump", 180_000, "2026-01-02T00:04:00Z")
    assert build_time_mapper([first, late]).map_interval(0, 60_000).time_quality == "conflict"


@pytest.mark.parametrize(
    "start,end",
    [(True, 1), (0, False), (0.0, 1), (-1, 1), (1, 1), (2, 1), (0, MAX_SAFE_INTEGER + 1)],
)
def test_invalid_monotonic_windows_fail_with_stable_error(start, end):
    for mapper in (build_time_mapper([]), build_time_mapper([anchor()])):
        with pytest.raises(ValueError, match="^invalid_monotonic_interval$"):
            mapper.map_interval(start, end)


def test_extreme_safe_integer_window_never_overflows_datetime():
    mapper = build_time_mapper([anchor()])
    result = mapper.map_interval(MAX_SAFE_INTEGER - 60_000, MAX_SAFE_INTEGER)
    assert result.time_quality == "unknown"
    assert "utc_out_of_range" in result.quality
    # A large monotonic uptime itself is valid when the anchored delta is small.
    large_clock = build_time_mapper([anchor(mono=MAX_SAFE_INTEGER)])
    result = large_clock.map_interval(MAX_SAFE_INTEGER - 60_000, MAX_SAFE_INTEGER)
    assert result.utc_start == "2026-01-02T00:00:00Z"


def test_utc_range_and_exclusive_upper_endpoint():
    low = build_time_mapper([anchor(utc="2000-01-01T00:00:00Z")])
    assert low.map_interval(0, 60_000).time_quality == "unknown"
    assert low.map_interval(60_000, 120_000).utc_start == "2000-01-01T00:00:00Z"
    high = build_time_mapper([anchor(mono=0, utc="2099-12-31T23:59:00Z")])
    assert high.map_interval(0, 60_000).utc_end == "2100-01-01T00:00:00Z"
    assert high.map_interval(60_000, 120_000).time_quality == "unknown"


@pytest.mark.parametrize(
    "field,value",
    [
        ("monotonic_ms", MAX_SAFE_INTEGER + 1),
        ("monotonic_ms", True),
        ("utc_at", "invalid"),
        ("utc_at", "2100-01-01T00:00:00Z"),
        ("uncertainty_ms", float("inf")),
        ("uncertainty_ms", -1.0),
    ],
)
def test_mutated_or_unvalidated_anchor_is_rejected_with_stable_error(field, value):
    item = anchor()
    setattr(item, field, value)
    with pytest.raises(ValueError, match="^invalid_time_anchor$"):
        build_time_mapper([item])


def test_large_finite_uncertainty_and_many_anchors_keep_mapper_bounded():
    baseline = datetime(2026, 1, 2, tzinfo=UTC)
    anchors = [
        anchor(
            str(i),
            i * 60_000,
            (baseline + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
            uncertainty=1e308,
        )
        for i in range(1000)
    ]
    mapper = build_time_mapper(anchors)
    assert mapper.anchor_ids == ("0",)
    anchors.clear()
    result = mapper.map_interval(0, 60_000)
    assert result.utc_start == "2026-01-02T00:00:00Z"
    assert result.time_uncertainty_ms is None


@pytest.mark.parametrize(
    "anchors",
    [
        [],
        [anchor()],
        [anchor("a"), anchor("b", 60_000, "2026-01-02T00:05:00Z")],
        [anchor(quality=["time_unknown"])],
    ],
)
def test_mapper_cache_round_trip_is_immutable_and_preserves_all_states(anchors):
    mapper = build_time_mapper(anchors)
    payload = json.loads(json.dumps(mapper.to_dict()))
    cached = BootTimeMapper.from_dict(payload)
    assert cached == mapper
    assert cached.map_interval(0, 60_000) == mapper.map_interval(0, 60_000)
    payload["anchor_ids"].append("later-mutation")
    payload["quality"].clear()
    assert cached == mapper


@pytest.mark.parametrize(
    "field,value",
    [
        ("_offset_us", True),
        ("_offset_us", None),
        ("_offset_us", 2**100),
        ("mapping_algorithm", "unrecognized-algorithm"),
        ("anchor_ids", ["a", "b"]),
        ("anchor_ids", []),
        ("quality", ["invented_quality"]),
        ("time_quality", "synced"),
        ("time_quality", "unknown"),
    ],
)
def test_mapper_cache_rejects_corrupt_or_obsolete_content(field, value):
    payload = build_time_mapper([anchor()]).to_dict()
    payload[field] = value
    with pytest.raises(ValueError, match="^invalid_time_mapper_cache$"):
        BootTimeMapper.from_dict(payload)


def test_exact_unix_microseconds_with_explicit_timezone():
    assert utc_to_us("2000-01-01T00:00:00Z") == 946_684_800_000_000
    assert us_to_utc(946_684_800_000_001) == "2000-01-01T00:00:00.000001Z"
    expected = "2026-01-02T00:00:00.123456Z"
    source = "2026-01-02T08:00:00.123456+08:00"
    assert us_to_utc(utc_to_us(source)) == expected
    assert utc_to_us(datetime.fromisoformat(source)) == utc_to_us(expected)
    assert us_to_utc(utc_to_us("2100-01-01T00:00:00Z")) == "2100-01-01T00:00:00Z"


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        "invalid",
        "2026-01-01T00:00:00",
        "1999-12-31T23:59:59Z",
        "2100-01-01T00:00:00.000001Z",
        datetime(2026, 1, 1),
    ],
)
def test_utc_conversion_rejects_naive_invalid_or_out_of_range_values(value):
    with pytest.raises(ValueError, match="^invalid_environment_utc$"):
        utc_to_us(value)


@pytest.mark.parametrize("value", [True, 1.0, 0, 2**100, None])
def test_reverse_utc_conversion_rejects_invalid_epoch_microseconds(value):
    with pytest.raises(ValueError, match="^invalid_environment_utc$"):
        us_to_utc(value)


def test_interval_union_does_not_double_count_duplicates_or_adjacent_boundaries():
    result = summarize_intervals([(0, 10), (0, 10), (10, 20)], 0, 30)
    assert result == {
        "gaps": [(20, 30)],
        "overlaps": [(0, 10)],
        "covered_us": 20,
        "overlap_us": 10,
        "interval_count": 3,
        "duplicate_interval_count": 1,
    }


def test_interval_clipping_nested_overlap_and_unordered_input():
    intervals = [(15, 25), (4, 6), (-20, 5), (0, 20), (40, 60), (-10, -5)]
    result = summarize_intervals(iter(intervals), 0, 30)
    assert result["gaps"] == [(25, 30)]
    assert result["overlaps"] == [(0, 6), (15, 20)]
    assert result["covered_us"] == 25
    assert result["overlap_us"] == 11
    assert result["interval_count"] == 4
    assert result["duplicate_interval_count"] == 0
    assert result == summarize_intervals(reversed(intervals), 0, 30)


def test_empty_or_outside_intervals_leave_whole_query_window_missing():
    result = summarize_intervals([(-10, 0), (20, 30)], 0, 20)
    assert result["gaps"] == [(0, 20)]
    assert result["overlaps"] == []
    assert result["covered_us"] == result["overlap_us"] == result["interval_count"] == 0
    assert result == summarize_intervals([], 0, 20)


def test_interval_sweep_matches_independent_discrete_coverage_oracle():
    random = Random(7341)
    for _ in range(100):
        intervals = []
        for _ in range(random.randrange(40)):
            left = random.randrange(-20, 40)
            intervals.append((left, left + random.randrange(1, 30)))
        counts = [sum(left <= point < right for left, right in intervals) for point in range(20)]
        result = summarize_intervals(intervals, 0, 20)
        assert result["covered_us"] == sum(count > 0 for count in counts)
        assert result["overlap_us"] == sum(count > 1 for count in counts)
        for point, count in enumerate(counts):
            assert any(left <= point < right for left, right in result["gaps"]) == (count == 0)
            assert any(left <= point < right for left, right in result["overlaps"]) == (count > 1)


@pytest.mark.parametrize("start,end", [(0, 0), (1, 0), (True, 10), (0.0, 10)])
def test_invalid_coverage_window_has_stable_error(start, end):
    with pytest.raises(ValueError, match="^invalid_interval_window$"):
        summarize_intervals([], start, end)


@pytest.mark.parametrize("interval", [(0, 0), (2, 1), (True, 2), (0, 1.0), (0,), None])
def test_invalid_coverage_interval_has_stable_error(interval):
    with pytest.raises(ValueError, match="^invalid_coverage_interval$"):
        summarize_intervals([interval], 0, 10)
