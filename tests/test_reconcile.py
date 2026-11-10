"""Handwritten synthetic expectations exercise an independent audit, without normalization."""

import copy
import json
from datetime import datetime

import pytest

from sleepscape.domain.reconcile import reconcile

DAY = "2025-01-02"
START = "2025-01-01T15:00:00Z"
END = "2025-01-01T15:04:01Z"


def ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def record(kind, key, data, timestamp=None, day=DAY):
    return {"kind": kind, "key": key, "date": day, "timestamp": timestamp, "data": data}


def sleep_pair():
    raw = {
        "calendarDate": DAY,
        "sleepTimeSeconds": 181,
        "napTimeSeconds": 0,
        "deepSleepSeconds": 60,
        "lightSleepSeconds": 120,
        "remSleepSeconds": 60,
        "awakeSleepSeconds": 60,
        "unmeasurableSleepSeconds": 0,
        "awakeCount": 1,
        "avgSleepStress": 23,
        "averageRespirationValue": 15,
        "averageSpO2Value": 97,
        "avgHeartRate": 52,
        "sleepScores": {"overall": {"value": 75}},
        "sleepStartTimestampGMT": ms(START),
        "sleepEndTimestampGMT": ms(END),
    }
    row = record(
        "sleep",
        DAY,
        {
            "sleep_seconds": 181,
            "nap_seconds": 0,
            "deep_seconds": 60,
            "light_seconds": 120,
            "rem_seconds": 60,
            "awake_seconds": 60,
            "unmeasurable_seconds": 0,
            "awake_count": 1,
            "average_stress": 23,
            "average_respiration": 15,
            "average_spo2": 97,
            "average_heart_rate": 52,
            "sleep_score": 75,
            "start_at": START,
            "end_at": END,
        },
        START,
    )
    return raw, row


def test_sleep_values_and_epoch_milliseconds_match_without_reinterpreting_source_precision():
    raw, row = sleep_pair()
    originals = copy.deepcopy((raw, row))
    result = reconcile("sleep", raw, [row])
    assert result["checked"] == result["matches"] == 17
    assert result["mismatches"] == result["missing_source"] == 0
    assert result["sleep_source_stage_minute_rounding_consistent"] == 1
    assert (raw, row) == originals


@pytest.mark.parametrize("wrong", ["2025-01-01T23:00:00Z", "2025-01-01T15:00:00", ms(START)])
def test_sleep_wrong_offset_missing_zone_or_unconverted_epoch_is_detected(wrong):
    raw, row = sleep_pair()
    row["data"]["start_at"] = wrong
    result = reconcile("sleep", raw, [row])
    assert result["sleep_time_mismatches"] == 1


@pytest.mark.parametrize("value", [-1, None, True, "52"])
def test_numeric_missing_sentinels_and_boolean_are_not_coerced_to_measurements(value):
    raw, row = sleep_pair()
    raw["avgHeartRate"], row["data"]["average_heart_rate"] = value, None
    assert reconcile("sleep", raw, [row])["mismatches"] == 0
    row["data"]["average_heart_rate"] = 0
    assert reconcile("sleep", raw, [row])["sleep_values_mismatches"] == 1


def test_absent_source_never_counts_as_match_and_missing_record_is_detected():
    raw, row = sleep_pair()
    del raw["avgHeartRate"]
    result = reconcile("sleep", raw, [row])
    assert result["missing_source"] == 1
    assert result["checked"] == 16
    assert reconcile("sleep", raw, [])["missing_records"] == 1


def test_unexplained_stage_difference_is_separate_from_mapping_mismatch():
    raw, row = sleep_pair()
    raw["deepSleepSeconds"], row["data"]["deep_seconds"] = 61, 61
    result = reconcile("sleep", raw, [row])
    assert result["mismatches"] == 0
    assert result["sleep_source_stage_unexplained_difference"] == 1


def hrv_pair():
    raw = {
        "hrvSummary": {
            "calendarDate": DAY,
            "lastNightAvg": 41,
            "weeklyAvg": 44,
            "lastNight5MinHigh": 65,
        },
        "hrvReadings": [{"readingTimeGMT": "2025-01-01T15:00:00", "hrvValue": 39}],
    }
    rows = [
        record(
            "hrv_daily",
            DAY,
            {"night_average_ms": 41, "weekly_average_ms": 44, "highest_5min_ms": 65},
        ),
        record("hrv_sample", START, {"hrv_ms": 39}, START),
    ]
    return raw, rows


def test_hrv_utc_and_source_sleep_date_and_night_average_are_independent_checks():
    raw, rows = hrv_pair()
    result = reconcile("hrv", raw, rows)
    assert result["checked"] == result["matches"] == 7
    rows[0]["data"]["night_average_ms"] = 44
    rows[1]["date"] = "2025-01-01"
    rows[1]["timestamp"] = "2025-01-01T07:00:00Z"
    result = reconcile("hrv", raw, rows)
    assert result["mismatches"] == 3
    assert result["hrv_summary_values_mismatches"] == 1
    assert result["hrv_sample_date_mismatches"] == 1
    assert result["hrv_sample_time_mismatches"] == 1


def test_hrv_summaries_wrapper_and_missing_readings_have_distinct_coverage():
    raw, rows = hrv_pair()
    result = reconcile("hrv_summaries", {"hrvSummaries": [raw["hrvSummary"]]}, rows[:1])
    assert result["matches"] == 4
    raw["hrvReadings"] = None
    result = reconcile("hrv", raw, rows)
    assert result["missing_source"] == 1
    assert result["unexpected_records"] == result["source_shape_errors"] == 0


def activity_pair():
    instant = "2025-01-01T23:30:00Z"
    raw = {
        "activityId": 321,
        "beginTimestamp": ms(instant),
        "startTimeGMT": "2025-01-01T23:30:00",
        "startTimeLocal": "2025-01-01T18:30:00",
        "activityTrainingLoad": 47,
        "duration": 1500,
        "elapsedDuration": 1600,
    }
    row = record(
        "activity",
        "321",
        {
            "activity_id": 321,
            "training_load": 47,
            "duration_seconds": 1500,
            "elapsed_seconds": 1600,
            "start_at": instant,
            "local_date": DAY,
        },
        instant,
        "2025-01-01",
    )
    return raw, row


def test_activity_pages_deduplicate_and_compare_api_local_day_not_configured_day():
    raw, row = activity_pair()
    result = reconcile("activities", {"pages": [[raw], [raw]], "pagination_complete": True}, [row])
    assert result["checked"] == result["matches"] == 7
    row["date"] = DAY
    row["data"]["training_load"] = 0
    result = reconcile("activities", [raw], [row])
    assert result["activity_source_date_mismatches"] == 1
    assert result["activity_values_mismatches"] == 1


def test_activity_partial_pages_never_verify_absence_and_missing_load_is_unverified():
    raw, row = activity_pair()
    result = reconcile("activities", {"pages": [[raw]], "pagination_complete": False}, [row])
    assert result["source_shape_errors"] == 1
    assert result["checked"] == 0
    del raw["activityTrainingLoad"]
    result = reconcile("activities", [raw], [row])
    assert result["missing_source"] == 1
    assert result["matches"] == 6


def test_activity_begin_milliseconds_precede_gmt_fallback():
    raw, row = activity_pair()
    raw["startTimeGMT"] = "2025-01-01T22:30:00"
    assert reconcile("activities", [raw], [row])["mismatches"] == 0
    del raw["beginTimestamp"]
    assert reconcile("activities", [raw], [row])["activity_time_mismatches"] == 2


def heart_pair():
    raw = {
        "calendarDate": DAY,
        "restingHeartRate": 51,
        "minHeartRate": 45,
        "maxHeartRate": 145,
        "heartRateValueDescriptors": [
            {"index": 1, "key": "timestamp"},
            {"index": 0, "key": "heartrate"},
        ],
        "heartRateValues": [[53, ms(START)], [-1, ms(END)]],
    }
    rows = [
        record("heart_rate_daily", DAY, {"resting_bpm": 51, "minimum_bpm": 45, "maximum_bpm": 145}),
        record("heart_rate_sample", "native:" + START, {"bpm": 53}, START),
        record("heart_rate_sample", "native:" + END, {"bpm": None}, END),
    ]
    return raw, rows


def test_heart_rate_decodes_descriptor_order_preserves_sentinel_and_source_partition():
    raw, rows = heart_pair()
    result = reconcile("heart_rate", raw, rows)
    assert result["checked"] == result["matches"] == 9
    rows[1]["data"]["bpm"] = ms(START)
    rows[2]["timestamp"] = "2025-01-01T23:04:01Z"
    result = reconcile("heart_rate", raw, rows)
    assert result["heart_rate_sample_values_mismatches"] == 1
    assert result["heart_rate_sample_time_mismatches"] == 1


def test_heart_rate_invalid_descriptors_and_missing_samples_do_not_pass():
    raw, rows = heart_pair()
    assert reconcile("heart_rate", raw, rows[:-1])["missing_records"] == 1
    raw["heartRateValueDescriptors"][0]["index"] = 0
    result = reconcile("heart_rate", raw, rows)
    assert result["source_shape_errors"] == 1
    assert result["checked"] == 3


def daily_pair():
    raw = {
        "calendarDate": DAY,
        "wellnessChronology": {"totalDurationInMillis": 7200000},
        "stress": {
            "totalDurationInMillis": 900000,
            "restDurationInMillis": 60000,
            "activityDurationInMillis": 120000,
            "uncategorizedDurationInMillis": 60000,
            "lowStressDurationInMillis": 180000,
            "mediumStressDurationInMillis": 240000,
            "highStressDurationInMillis": 240000,
            "avgLevel": 23,
        },
        "movement": {"steps": {"value": 1234}},
        "intensityMinutes": {"moderate": 20, "vigorous": 10},
        "includesWellnessData": True,
        "includesActivityData": False,
    }
    row = record(
        "daily_summary",
        DAY,
        {
            "wellness_duration_seconds": 7200,
            "total_stress_seconds": 900,
            "rest_stress_seconds": 60,
            "activity_stress_seconds": 120,
            "uncategorized_stress_seconds": 60,
            "low_stress_seconds": 180,
            "medium_stress_seconds": 240,
            "high_stress_seconds": 240,
            "average_stress": 23,
            "steps": 1234,
            "moderate_minutes": 20,
            "vigorous_minutes": 10,
            "includes_wellness_data": True,
            "includes_activity_data": False,
        },
    )
    return raw, row


def test_daily_milliseconds_convert_but_intensity_minutes_and_booleans_do_not():
    raw, row = daily_pair()
    assert reconcile("daily_summaries", {"data": [raw]}, [row])["matches"] == 14
    row["data"]["wellness_duration_seconds"] = 7200000
    row["data"]["moderate_minutes"] = 0.02
    row["data"]["includes_wellness_data"] = 1
    result = reconcile("daily_summaries", {"data": [raw]}, [row])
    assert result["daily_duration_units_mismatches"] == 1
    assert result["daily_values_mismatches"] == 1
    assert result["daily_flags_mismatches"] == 1


def test_counts_only_and_extra_rows_and_unsupported_shapes():
    raw, row = sleep_pair()
    extra = record("sleep", "synthetic-private-marker", {})
    result = reconcile("sleep_summaries", [raw], [row, extra])
    assert result["unexpected_records"] == 1
    assert all(type(value) is int for value in result.values())
    assert DAY not in json.dumps(result) and "synthetic-private-marker" not in json.dumps(result)
    assert reconcile("sleep", None, [row])["checked"] == 0
    assert reconcile("daily_summaries", [], [row])["source_shape_errors"] == 1
    assert reconcile("unsupported", {}, [row])["unsupported_resource"] == 1


def test_reconciliation_does_not_call_normalizer(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("audit must not validate normalization against itself")

    monkeypatch.setattr("sleepscape.domain.normalize.normalize", forbidden)
    raw, row = sleep_pair()
    assert reconcile("sleep", raw, [row])["mismatches"] == 0
