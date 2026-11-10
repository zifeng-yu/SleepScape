"""View prerequisites use actual sleep instants and current source status."""

from sleepscape.services.verification import display_readiness


def row(resource, kind, day, data, timestamp=None):
    return {"resource": resource, "kind": kind, "date": day, "data": data, "timestamp": timestamp}


def test_cross_midnight_samples_are_associated_by_instant_not_partition_day():
    records = [
        row(
            "sleep",
            "sleep",
            "2026-01-03",
            {
                "start_at": "2026-01-02T15:00:00Z",
                "end_at": "2026-01-02T23:00:00Z",
                "average_heart_rate": 55,
                "stage_timeline_available": False,
            },
        ),
        row("hrv", "hrv_daily", "2026-01-03", {"night_average_ms": 50}),
        row(
            "heart_rate",
            "heart_rate_sample",
            "2026-01-02",
            {"bpm": 55},
            "2026-01-02T15:00:00+00:00",
        ),
        row("hrv", "hrv_sample", "2026-01-03", {"hrv_ms": 50}, "2026-01-02T18:00:00+00:00"),
        row(
            "heart_rate",
            "heart_rate_sample",
            "2026-01-02",
            {"bpm": 80},
            "2026-01-02T14:00:00+00:00",
        ),
    ]
    latest = {(r["resource"], r["date"]): {"status": "ok"} for r in records}
    result = display_readiness(records, [], "2026-01-03", "2026-01-03", latest)
    assert result["sleep_windows"][0]["sample_counts"] == {
        "heart_rate_sample": 1,
        "hrv_sample": 1,
    }
    assert result["available_days"]["stage_timeline_available_days"] == 0
    assert result["available_days"]["night_average_hrv_days"] == 1
    assert result["available_days"]["full_30_day_baseline_days"] == 0
    assert result["available_days"]["percentage_displayable_days"] == 0
    latest[("heart_rate", "2026-01-02")]["status"] = "failed"
    result = display_readiness(records, [], "2026-01-03", "2026-01-03", latest)
    assert result["sleep_windows"][0]["sample_counts"]["heart_rate_sample"] == 0
    assert result["sleep_windows"][0]["time_coverage_ratio"] is None


def test_missing_sleep_does_not_produce_a_fake_window_or_zero_heart_rate():
    result = display_readiness([], [], "2026-01-03", "2026-01-04", {})
    assert result["expected_sleep_days"] == 2
    assert not any(result["available_days"].values())
    assert all(not night["valid_window"] for night in result["sleep_windows"])
