"""Synthetic fixtures only; no account, credentials, or network access."""

import copy
import json
from datetime import UTC, datetime

import pytest

from sleepscape.domain.models import DataRequest
from sleepscape.domain.normalize import normalize


def request(resource):
    return DataRequest(resource, "synthetic", "/unused", start_date="2025-01-02")


def epoch(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def of_kind(batch, kind):
    return [record for record in batch.records if record["kind"] == kind]


def codes(batch):
    return {issue["code"] for issue in batch.issues}


def test_sleep_crosses_midnight_and_does_not_invent_stages():
    payload = {
        "calendarDate": "2025-01-02",
        "sleepStartTimestampGMT": epoch("2025-01-01T15:00:00"),
        "sleepEndTimestampGMT": epoch("2025-01-01T23:00:00"),
        "sleepStartTimestampLocal": epoch("2025-01-01T23:00:00"),
        "sleepTimeSeconds": 27000,
        "deepSleepSeconds": 6000,
        "lightSleepSeconds": 15000,
        "remSleepSeconds": 6000,
        "awakeSleepSeconds": 1800,
    }
    original = copy.deepcopy(payload)
    batch = normalize(request("sleep"), payload)
    assert payload == original
    record = batch.records[0]
    assert record["date"] == "2025-01-02"
    assert record["timestamp"] == "2025-01-01T15:00:00Z"
    assert record["data"]["sleep_seconds"] == 27000
    assert record["data"]["source"]["sleep_seconds"] == "sleepTimeSeconds"
    assert not of_kind(batch, "sleep_stage")
    assert "sleep_stage_timeline_unavailable" in codes(batch)


def test_nap_is_a_separate_record_and_not_added_to_main_sleep():
    batch = normalize(
        request("sleep"),
        {
            "calendarDate": "2025-01-02",
            "sleepTimeSeconds": 24000,
            "dailyNapDTOS": [
                {
                    "calendarDate": "2025-01-02",
                    "napTimeSec": 900,
                    "napStartTimestampGMT": "2025-01-02T05:00:00",
                    "napEndTimestampGMT": "2025-01-02T05:15:00",
                    "napSource": 0,
                }
            ],
        },
    )
    assert of_kind(batch, "sleep")[0]["data"]["sleep_seconds"] == 24000
    assert of_kind(batch, "nap")[0]["data"]["duration_seconds"] == 900


def test_sleep_minute_aggregation_difference_is_informational_without_rewriting_values():
    batch = normalize(
        request("sleep"),
        {
            "calendarDate": "2025-01-02",
            "sleepTimeSeconds": 181,
            "deepSleepSeconds": 60,
            "lightSleepSeconds": 120,
            "remSleepSeconds": 60,
        },
    )
    data = batch.records[0]["data"]
    assert data["sleep_seconds"] == 181
    assert data["derived_classified_sleep_seconds"] == 240
    assert "sleep_summary_totals_differ" not in codes(batch)
    issue = next(i for i in batch.issues if i["code"] == "sleep_summary_minute_rounding_consistent")
    assert issue["severity"] == "info"
    assert batch.metrics["normalization_version"] == "2"


@pytest.mark.parametrize(
    "total,stages",
    [(180, [60, 120, 60]), (239, [61, 119, 60]), (241, [60, 120, 60]), (120, [60, 120, 60])],
)
def test_unexplained_sleep_total_differences_remain_warnings(total, stages):
    batch = normalize(
        request("sleep"),
        {
            "calendarDate": "2025-01-02",
            "sleepTimeSeconds": total,
            "deepSleepSeconds": stages[0],
            "lightSleepSeconds": stages[1],
            "remSleepSeconds": stages[2],
        },
    )
    assert "sleep_summary_totals_differ" in codes(batch)
    assert "sleep_summary_minute_rounding_consistent" not in codes(batch)


def test_real_intervals_only_unknown_stage_is_not_guessed():
    batch = normalize(
        request("sleep"),
        {
            "calendarDate": "2025-01-02",
            "sleepLevels": [
                {
                    "startGMT": "2025-01-01T16:00:00",
                    "endGMT": "2025-01-01T16:01:00",
                    "activityLevel": "deep",
                },
                {
                    "startGMT": "2025-01-01T16:01:00",
                    "endGMT": "2025-01-01T16:02:00",
                    "activityLevel": 123,
                },
                {
                    "startGMT": "2025-01-01T16:03:00",
                    "endGMT": "2025-01-01T16:02:00",
                    "activityLevel": "light",
                },
            ],
        },
    )
    stages = of_kind(batch, "sleep_stage")
    assert [r["data"]["stage"] for r in stages] == ["deep", "unknown"]
    assert stages[1]["data"]["raw_stage"] == 123
    assert "invalid_sleep_stage_interval" in codes(batch)


@pytest.mark.parametrize("invalid", [epoch("2025-01-01T16:00:00") // 1000, "not-a-date", True])
def test_timestamp_unit_errors_are_not_silently_rescaled(invalid):
    batch = normalize(
        request("sleep"), {"calendarDate": "2025-01-02", "sleepStartTimestampGMT": invalid}
    )
    assert batch.records[0]["timestamp"] is None
    assert batch.records[0]["data"]["raw"]["sleepStartTimestampGMT"] == invalid
    assert codes(batch) & {"invalid_timestamp", "invalid_timestamp_unit_or_range"}


def test_hrv_gmt_naive_is_utc_and_nightly_and_weekly_are_distinct():
    batch = normalize(
        request("hrv"),
        {
            "hrvSummary": {"calendarDate": "2025-01-02", "lastNightAvg": 51, "weeklyAvg": 47},
            "hrvReadings": [
                {
                    "hrvValue": 54,
                    "readingTimeGMT": "2025-01-01T16:00:00.125",
                    "readingTimeLocal": "2025-01-02T00:00:00.125",
                }
            ],
        },
    )
    daily = of_kind(batch, "hrv_daily")[0]
    sample = of_kind(batch, "hrv_sample")[0]
    assert daily["data"]["night_average_ms"] == 51
    assert daily["data"]["weekly_average_ms"] == 47
    assert sample["timestamp"] == "2025-01-01T16:00:00.125000Z"
    assert sample["date"] == "2025-01-02"
    assert sample["data"]["aggregation_window_seconds"] is None


def test_hrv_summaries_requires_documented_wrapper():
    valid = normalize(request("hrv_summaries"), {"hrvSummaries": [{"calendarDate": "2025-01-02"}]})
    invalid = normalize(request("hrv_summaries"), [{"calendarDate": "2025-01-02"}])
    assert len(valid.records) == 1
    assert not invalid.records
    assert "unexpected_shape" in codes(invalid)
    assert invalid.metrics["payload_shape_valid"] is False


def test_hrv_samples_keep_source_night_partition_across_midnight():
    batch = normalize(
        request("hrv"),
        {
            "hrvSummary": {"calendarDate": "2025-01-02"},
            "hrvReadings": [
                {"hrvValue": 45, "readingTimeGMT": "2025-01-01T15:30:00"},
                {"hrvValue": 46, "readingTimeGMT": "2025-01-01T16:30:00"},
            ],
        },
    )
    samples = of_kind(batch, "hrv_sample")
    assert [sample["date"] for sample in samples] == ["2025-01-02", "2025-01-02"]
    assert [sample["data"]["local_date"] for sample in samples] == ["2025-01-01", "2025-01-02"]
    assert all(sample["data"]["source_date"] == "2025-01-02" for sample in samples)


def test_descriptor_column_order_sentinels_and_connect_date():
    batch = normalize(
        request("heart_rate"),
        {
            "calendarDate": {"localDate": "2025-01-02"},
            "startTimestampGMT": {"date": "2025-01-01T16:00:00Z"},
            "heartRateValueDescriptors": [
                {"index": 1, "key": "timestamp"},
                {"index": 0, "key": "heartrate"},
            ],
            "heartRateValues": [
                [62, epoch("2025-01-01T16:00:00")],
                [-1, epoch("2025-01-01T16:02:00")],
                [None, epoch("2025-01-01T16:04:00")],
            ],
        },
    )
    samples = of_kind(batch, "heart_rate_sample")
    assert [sample["data"]["bpm"] for sample in samples] == [62, None, None]
    assert samples[1]["data"]["raw"]["heartrate"] == -1
    assert samples[1]["data"]["quality"]["bpm"] == "negative_or_sentinel"
    assert samples[2]["data"]["quality"]["bpm"] == "null"
    daily = of_kind(batch, "heart_rate_daily")[0]["data"]
    assert daily["sample_count"] == 3
    assert daily["valid_sample_count"] == 1
    assert daily["coverage_fraction"] is None


@pytest.mark.parametrize(
    "descriptors", [None, [], [{"index": 0, "key": "timestamp"}, {"index": 0, "key": "heartrate"}]]
)
def test_ambiguous_descriptor_does_not_assume_fixed_columns(descriptors):
    batch = normalize(
        request("heart_rate"),
        {
            "calendarDate": "2025-01-02",
            "heartRateValueDescriptors": descriptors,
            "heartRateValues": [[epoch("2025-01-01T16:00:00"), 60]],
        },
    )
    assert not of_kind(batch, "heart_rate_sample")
    assert any(issue["severity"] == "error" for issue in batch.issues)


def test_zero_heart_rate_is_invalid_but_zero_steps_is_valid():
    heart = normalize(request("heart_rate"), {"calendarDate": "2025-01-02", "restingHeartRate": 0})
    steps = normalize(request("steps"), [{"calendarDate": "2025-01-02", "totalSteps": 0}])
    assert heart.records[0]["data"]["resting_bpm"] is None
    assert steps.records[0]["data"]["steps"] == 0
    assert steps.records[0]["data"]["distance_meters"] is None


def test_stress_sentinel_is_not_zero_and_duration_is_seconds():
    batch = normalize(
        request("stress"),
        [
            {
                "calendarDate": "2025-01-02",
                "values": {
                    "overallStressLevel": -2,
                    "highStressDuration": 3600,
                    "restStressDuration": 0,
                },
            }
        ],
    )
    data = batch.records[0]["data"]
    assert data["average_stress"] is None
    assert data["raw"]["values.overallStressLevel"] == -2
    assert data["high_seconds"] == 3600
    assert data["rest_seconds"] == 0


def test_v2_milliseconds_vs_legacy_seconds_and_intensity_minutes():
    v2 = normalize(
        request("daily_summaries"),
        {
            "data": [
                {
                    "calendarDate": "2025-01-02",
                    "stress": {"restDurationInMillis": 42000},
                    "intensityMinutes": {"vigorous": 4},
                    "wellnessChronology": {"totalDurationInMillis": 60000},
                }
            ]
        },
    )
    legacy = normalize(
        request("daily"),
        {
            "calendarDate": "2025-01-02",
            "restStressDuration": 42,
            "vigorousIntensityMinutes": 4,
            "lastSyncTimestampGMT": None,
            "bodyBatteryAtWakeTime": 70,
        },
    )
    assert v2.records[0]["data"]["rest_stress_seconds"] == 42
    assert legacy.records[0]["data"]["rest_stress_seconds"] == 42
    assert (
        v2.records[0]["data"]["vigorous_minutes"]
        == legacy.records[0]["data"]["vigorous_minutes"]
        == 4
    )
    assert legacy.records[0]["kind"] == "daily_summary_legacy"
    assert legacy.records[0]["data"]["last_sync_at"] is None


def test_spo2_hourly_is_not_claimed_as_raw_minute_series():
    batch = normalize(
        request("spo2"),
        {
            "calendarDate": "2025-01-02",
            "spO2SingleValues": None,
            "spO2HourlyAveragesDescriptorList": [
                {"spo2ValueDescIndex": 1, "spo2ValueDescKey": "timestamp"},
                {"spo2ValueDescIndex": 0, "spo2ValueDescKey": "spo2Level"},
            ],
            "spO2HourlyAverages": [[97, epoch("2025-01-01T16:00:00")]],
        },
    )
    assert len(of_kind(batch, "spo2_daily")) == 1
    sample = of_kind(batch, "spo2_sample")[0]["data"]
    assert sample["percent"] == 97
    assert sample["series_type"] == "hourly_average"
    assert sample["aggregation_window_seconds"] == 3600
    assert "sample_series_unavailable" in codes(batch)


def test_native_spo2_uses_reading_descriptor_and_source_plot_flag():
    batch = normalize(
        request("spo2"),
        {
            "calendarDate": "2025-01-02",
            "spO2SingleValuesDescriptorList": [
                {"spo2ValueDescriptorIndex": 0, "spo2ValueDescriptorKey": "timestamp"},
                {"spo2ValueDescriptorIndex": 1, "spo2ValueDescriptorKey": "spo2Reading"},
                {"spo2ValueDescriptorIndex": 2, "spo2ValueDescriptorKey": "singleReadingPlottable"},
            ],
            "spO2SingleValues": [[epoch("2025-01-01T16:00:00"), 96, False]],
        },
    )
    sample = of_kind(batch, "spo2_sample")[0]["data"]
    assert sample["percent"] == 96
    assert sample["quality"]["percent"] == "source_not_plottable"
    assert of_kind(batch, "spo2_daily")[0]["data"]["valid_sample_count"] == 0


def activity(**extra):
    return {
        "activityId": 123,
        "activityType": {"typeKey": "cycling"},
        "beginTimestamp": epoch("2025-01-01T15:50:00"),
        "startTimeGMT": "2025-01-01 15:50:00",
        "startTimeLocal": "2025-01-01 23:50:00",
        "duration": 900,
        "elapsedDuration": 1200,
        "activityTrainingLoad": 0,
        **extra,
    }


def test_activity_actual_end_overrides_elapsed_and_local_date_uses_start():
    batch = normalize(
        request("activities"),
        {"pages": [[activity(endTimeGMT="2025-01-01 16:15:00")]], "pagination_complete": True},
    )
    record = batch.records[0]
    assert record["date"] == "2025-01-01"
    assert record["data"]["end_at"] == "2025-01-01T16:15:00Z"
    assert record["data"]["training_load"] == 0
    assert record["data"]["end_at_derived"] is False
    assert record["data"]["sport_type"] == "cycling"


@pytest.mark.parametrize(
    "utc_start,source_start,source_day,configured_day",
    [
        ("2025-01-01T16:30:00", "2025-01-01 08:30:00", "2025-01-01", "2025-01-02"),
        ("2025-01-01T15:30:00", "2025-01-02 00:30:00", "2025-01-02", "2025-01-01"),
    ],
)
def test_activity_partition_uses_source_day_without_guessing_source_timezone(
    utc_start,
    source_start,
    source_day,
    configured_day,
):
    batch = normalize(
        request("activities"),
        [
            activity(
                beginTimestamp=epoch(utc_start),
                startTimeGMT=utc_start,
                startTimeLocal=source_start,
            )
        ],
    )
    record = batch.records[0]
    assert record["date"] == source_day
    assert record["timestamp"] == utc_start + "Z"
    assert record["data"]["source_date"] == record["data"]["source_calendar_date"] == source_day
    assert record["data"]["local_date"] == configured_day
    assert record["data"]["source_start_local"] == source_start
    assert record["data"]["timezone"] == "Asia/Shanghai"
    assert record["data"]["date_basis"] == "source_local_start"
    assert "source_timezone" not in record["data"]
    assert "activity_source_date_differs_from_configured_date" in codes(batch)
    assert batch.metrics["payload_shape_valid"] is True


def test_activity_source_day_remains_stable_when_display_timezone_changes():
    batch = normalize(
        request("activities"),
        [
            activity(
                beginTimestamp=epoch("2025-01-01T16:30:00"),
                startTimeLocal="2025-01-02 00:30:00",
            )
        ],
        timezone="Europe/London",
    )
    record = batch.records[0]
    assert record["date"] == "2025-01-02"
    assert record["data"]["local_date"] == "2025-01-01"
    assert record["data"]["timezone"] == "Europe/London"


@pytest.mark.parametrize("source_start", [None, "invalid", "2025-02-30 00:00:00", "2025-01-01", 1])
def test_invalid_activity_source_date_falls_back_explicitly_to_configured_day(source_start):
    batch = normalize(request("activities"), [activity(startTimeLocal=source_start)])
    record = batch.records[0]
    assert record["date"] == record["data"]["local_date"] == "2025-01-01"
    assert record["data"]["source_date"] is None
    assert record["data"]["source_calendar_date"] is None
    assert record["data"]["source_start_local"] == source_start
    assert record["data"]["date_basis"] == "configured_timezone_fallback"
    assert record["data"]["quality"]["date"] == "configured_timezone_fallback"
    assert "activity_source_date_unavailable" in codes(batch)


def test_activity_without_any_assignable_day_cannot_become_a_zero_load_day():
    batch = normalize(
        request("activities"),
        [
            activity(
                startTimeLocal=None,
                beginTimestamp=None,
                startTimeGMT=None,
            )
        ],
    )
    assert not batch.records
    assert batch.metrics["payload_shape_valid"] is False
    assert "activity_date_unavailable" in codes(batch)


def test_activity_derived_end_uses_elapsed_not_timer_and_preserves_missing_load():
    batch = normalize(request("activities"), [activity(activityTrainingLoad=None)])
    data = batch.records[0]["data"]
    assert data["end_at"] == "2025-01-01T16:10:00Z"
    assert data["end_at_derived"] is True
    assert data["training_load"] is None
    absent = normalize(request("activities"), [activity(elapsedDuration=None)])
    assert absent.records[0]["data"]["end_at"] is None


def test_partial_pages_produce_no_activities_and_duplicate_ids_are_not_summed():
    partial = normalize(
        request("activities"), {"pages": [[activity()]], "pagination_complete": False}
    )
    assert partial.records == []
    assert "incomplete_activity_pagination" in codes(partial)
    complete = normalize(
        request("activities"), {"pages": [[activity()], [activity()]], "pagination_complete": True}
    )
    assert len(complete.records) == 1
    assert "duplicate_activity_id" in codes(complete)


def test_missing_activity_identity_is_invalid_instead_of_colliding_on_day():
    batch = normalize(request("activities"), [activity(activityId=None), activity(activityId=None)])
    assert not batch.records
    assert batch.metrics["payload_shape_valid"] is False
    assert "invalid_activity_id" in codes(batch)


@pytest.mark.parametrize(
    "resource,payload,state",
    [
        ("hrv", None, "empty"),
        ("sleep", {}, "empty_object"),
        ("wellness_activities", [], "empty_list"),
        ("activities_page", [activity()], "raw_only"),
    ],
)
def test_empty_or_raw_only_payloads_do_not_create_zero_records(resource, payload, state):
    batch = normalize(request(resource), payload)
    assert batch.records == []
    assert batch.metrics["payload_state"] == state


def test_issues_contain_no_input_values_and_bad_timezone_is_safe():
    secret_like_value = "synthetic-private-marker"
    batch = normalize(
        request("sleep"),
        {"calendarDate": secret_like_value, "sleepStartTimestampGMT": secret_like_value},
    )
    assert secret_like_value not in json.dumps(batch.issues)
    assert all(set(issue) == {"severity", "code", "field"} for issue in batch.issues)
    invalid = normalize(request("sleep"), {}, timezone="not/a/zone")
    assert "invalid_timezone" in codes(invalid)


@pytest.mark.parametrize(
    "resource,payload",
    [
        ("hrv", {"error": "synthetic"}),
        ("heart_rate", {"unexpected": []}),
        ("daily_summaries", {"data": [{"unexpected": []}]}),
        ("steps", [{"unexpected": []}]),
        ("sleep", []),
    ],
)
def test_unexpected_shape_cannot_be_mistaken_for_confirmed_empty(resource, payload):
    batch = normalize(request(resource), payload)
    assert not batch.records
    assert batch.metrics["payload_shape_valid"] is False


def test_conflicting_sample_identity_invalidates_batch_instead_of_silent_overwrite():
    batch = normalize(
        request("hrv"),
        {
            "hrvSummary": {"calendarDate": "2025-01-02"},
            "hrvReadings": [
                {"readingTimeGMT": "2025-01-01T16:00:00", "hrvValue": 41},
                {"readingTimeGMT": "2025-01-01T16:00:00", "hrvValue": 42},
            ],
        },
    )
    assert batch.metrics["payload_shape_valid"] is False
    assert "conflicting_duplicate_record" in codes(batch)
