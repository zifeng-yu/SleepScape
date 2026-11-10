"""Display resolution tests use observations generated here, never personal samples."""

from datetime import UTC, datetime, timedelta

import pytest

from sleepscape.domain.downsample import minmax_downsample

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(seconds=100)


def point(offset, value):
    return {
        "timestamp": (START + timedelta(seconds=offset)).isoformat(),
        "value": value,
        "quality": [],
    }


def test_minmax_keeps_actual_times_extrema_and_counts_without_filling_gaps():
    points = [point(i, (-i if i % 2 else i)) for i in range(20)]
    points.extend(point(i, i * 2) for i in range(80, 100))
    result = minmax_downsample(points, START, END, 10)
    selected = result["points"]
    assert len(selected) <= 10
    assert {row["value"] for row in selected} == {-19, 18, 160, 198}
    assert all(row["timestamp"] in {item["timestamp"] for item in points} for row in selected)
    assert [row["timestamp"] for row in selected] == sorted(row["timestamp"] for row in selected)
    assert all(row["bucket_valid_count"] == 20 for row in selected)
    assert result["display_resolution"] == {
        "method": "minmax",
        "original_count": 40,
        "valid_count": 40,
        "returned_count": 4,
        "bucket_seconds": 20,
    }
    assert all("bucket_valid_count" not in row for row in points)


def test_sparse_points_invalid_values_and_odd_budget_are_not_interpolated():
    points = [
        point(2, 0),
        point(99, 4),
        point(101, 5),
        point(3, None),
        point(4, float("nan")),
        point(5, True),
    ]
    result = minmax_downsample(points, START, END, 3)
    assert [row["value"] for row in result["points"]] == [0, 4]
    assert result["display_resolution"]["method"] == "none"
    assert result["display_resolution"]["original_count"] == 6
    many = minmax_downsample([point(i, i) for i in range(100)], START, END, 3)
    assert len(many["points"]) == 2
    assert [row["value"] for row in many["points"]] == [0, 99]


def test_flat_buckets_keep_an_actual_point_and_end_boundary_is_included():
    result = minmax_downsample([point(i, 2) for i in range(101)], START, END, 4)
    assert len(result["points"]) == 2
    assert sum(row["bucket_valid_count"] for row in result["points"]) == 101


@pytest.mark.parametrize("budget", [0, 1, 2001, True])
def test_point_budget_rejected(budget):
    with pytest.raises(ValueError, match="invalid_max_points"):
        minmax_downsample([], START, END, budget)


def test_naive_and_invalid_timestamp_never_sneak_into_valid_points():
    invalid = [
        {"timestamp": "2026-01-01T00:00:00", "value": 1},
        {"timestamp": "not a time", "value": 2},
    ]
    assert minmax_downsample(invalid, START, END)["points"] == []
    with pytest.raises(ValueError):
        minmax_downsample([], START.replace(tzinfo=None), END)
