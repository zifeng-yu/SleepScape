"""Pure, conservative normalization of the documented Garmin Personal API.

Source values and field mappings survive normalization. Invalid measurements become
None, never zero; missing timestamps never acquire an invented sampling interval.
Raw responses are persisted separately by the caller, including unmapped fields.
"""

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sleepscape.domain.models import DataRequest, JsonValue, NormalizedBatch

VERSION = "2"
_MISSING = object()


def _get(obj: dict[str, Any], path: str) -> Any:
    value: Any = obj
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _Normalizer:
    def __init__(self, request: DataRequest, timezone: str):
        self.request = request
        self.zone = ZoneInfo(timezone)
        self.batch = NormalizedBatch(
            metrics={
                "normalization_version": VERSION,
                "payload_shape_valid": True,
            }
        )
        self.issue_keys: set[tuple[str, str, str]] = set()
        self.record_keys: dict[tuple[str, str], dict[str, Any]] = {}

    def issue(self, code: str, field: str, severity: str = "warning") -> None:
        if severity == "error":
            self.batch.metrics["payload_shape_valid"] = False
        key = (severity, code, field)
        if key not in self.issue_keys:
            self.issue_keys.add(key)
            self.batch.issues.append({"severity": severity, "code": code, "field": field})

    @staticmethod
    def data() -> dict[str, Any]:
        return {"source": {}, "raw": {}, "quality": {}}

    def field(
        self,
        data: dict[str, Any],
        obj: dict[str, Any],
        name: str,
        path: str,
        *,
        numeric: bool = True,
        positive: bool = False,
        maximum: float | None = None,
        divisor: float = 1,
    ) -> Any:
        value = _get(obj, path)
        data["source"][name] = path
        if value is _MISSING:
            data[name] = None
            data["quality"][name] = "missing"
            return None
        data["raw"][path] = value
        problem = None
        if value is None:
            problem = "null"
        elif numeric:
            if not _finite(value):
                problem = "invalid_number"
            elif value < 0:
                problem = "negative_or_sentinel"
            elif positive and value == 0:
                problem = "nonpositive_measurement"
            elif maximum is not None and value > maximum:
                problem = "outside_scale"
        if problem:
            data[name] = None
            data["quality"][name] = problem
            if problem != "null":
                self.issue(problem, path)
            return None
        result = value / divisor if numeric and divisor != 1 else value
        data[name] = result
        data["quality"].pop(name, None)
        return result

    def fields(self, data: dict[str, Any], obj: dict[str, Any], mapping: dict[str, str]) -> None:
        for name, path in mapping.items():
            self.field(data, obj, name, path)

    def instant(self, value: Any, field: str, *, local: bool = False) -> datetime | None:
        if value is _MISSING or value is None:
            return None
        if isinstance(value, dict):
            value = value.get("date")
        try:
            if _finite(value):
                # Numeric fields in the supported payloads are epoch milliseconds.
                # A seconds-shaped value is rejected rather than silently rescaled.
                if not 946684800000 <= value < 4133980800000:
                    self.issue("invalid_timestamp_unit_or_range", field)
                    return None
                if local:
                    self.issue("ambiguous_local_numeric_timestamp", field)
                    return None
                parsed = datetime.fromtimestamp(value / 1000, UTC)
            elif isinstance(value, str) and ("T" in value or " " in value):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=self.zone if local else UTC)
                    if local:
                        self.issue("configured_timezone_used", field, "info")
            else:
                raise ValueError
            return parsed.astimezone(UTC)
        except (ValueError, TypeError, OverflowError, OSError):
            self.issue("invalid_timestamp", field)
            return None

    def time_field(
        self,
        data: dict[str, Any],
        obj: dict[str, Any],
        name: str,
        path: str,
        *,
        local: bool = False,
    ) -> datetime | None:
        raw = self.field(data, obj, name, path, numeric=False)
        parsed = self.instant(raw, path, local=local)
        data[name] = self.iso(parsed)
        if raw is not None and parsed is None:
            data["quality"][name] = "invalid_timestamp"
        return parsed

    @staticmethod
    def iso(value: datetime | None) -> str | None:
        return value.isoformat().replace("+00:00", "Z") if value else None

    def day(self, obj: dict[str, Any], field: str = "calendarDate") -> str | None:
        raw = obj.get(field)
        if isinstance(raw, dict):
            raw = raw.get("localDate")
        if raw is None:
            return None
        try:
            if not isinstance(raw, str) or len(raw) != 10:
                raise ValueError
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            self.issue("invalid_calendar_date", field)
            return None

    def local_day(self, value: datetime | None) -> str | None:
        return value.astimezone(self.zone).date().isoformat() if value else None

    def sample_day(self, source_day: str | None) -> str | None:
        """Keep endpoint partitions independent of the sample's wall-clock date."""
        if source_day:
            return source_day
        if self.request.end_date in {None, self.request.start_date}:
            return self.day({"calendarDate": self.request.start_date})
        return None

    def add(
        self,
        kind: str,
        data: dict[str, Any],
        day: str | None,
        timestamp: datetime | None = None,
        key: str | None = None,
    ) -> None:
        if key is None:
            key = day or self.iso(timestamp)
        if key is None:
            key = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:20]
            self.issue("record_without_calendar_date", "calendarDate")
        record = {
            "kind": kind,
            "key": str(key),
            "date": day,
            "timestamp": self.iso(timestamp),
            "data": data,
        }
        identity = (kind, str(key))
        if identity in self.record_keys:
            if record != self.record_keys[identity]:
                self.issue("conflicting_duplicate_record", "records", "error")
            else:
                self.issue("duplicate_record", "records", "info")
            return
        self.record_keys[identity] = record
        self.batch.records.append(record)

    def window(
        self,
        data: dict[str, Any],
        obj: dict[str, Any],
        start: str,
        end: str,
    ) -> tuple[datetime | None, datetime | None]:
        begin = self.time_field(data, obj, "start_at", start)
        finish = self.time_field(data, obj, "end_at", end)
        if begin and finish and finish <= begin:
            self.issue("nonpositive_time_window", start)
            data["quality"]["time_window"] = "nonpositive_time_window"
        return begin, finish

    def objects(self, value: Any, field: str) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            self.issue("unexpected_shape", field, "error")
            return []
        result = []
        for item in value:
            if isinstance(item, dict) and item:
                result.append(item)
            else:
                self.issue("invalid_list_item", field, "error")
        return result

    def sleep(self, obj: dict[str, Any]) -> None:
        if not any(
            k in obj for k in ("calendarDate", "sleepTimeSeconds", "sleepStartTimestampGMT")
        ):
            self.issue("unexpected_shape", "sleep", "error")
            return
        data = self.data()
        self.fields(
            data,
            obj,
            {
                "sleep_seconds": "sleepTimeSeconds",
                "nap_seconds": "napTimeSeconds",
                "deep_seconds": "deepSleepSeconds",
                "light_seconds": "lightSleepSeconds",
                "rem_seconds": "remSleepSeconds",
                "awake_seconds": "awakeSleepSeconds",
                "unmeasurable_seconds": "unmeasurableSleepSeconds",
                "awake_count": "awakeCount",
                "average_stress": "avgSleepStress",
                "average_respiration": "averageRespirationValue",
                "average_spo2": "averageSpO2Value",
                "sleep_score": "sleepScores.overall.value",
                "sleep_version": "sleepVersion",
            },
        )
        self.field(data, obj, "average_heart_rate", "avgHeartRate", positive=True)
        for name, path in {
            "confirmed": "sleepWindowConfirmed",
            "confirmation_type": "sleepWindowConfirmationType",
            "from_device": "sleepFromDevice",
            "breathing_disruption": "breathingDisruptionSeverity",
            "source_start_local": "sleepStartTimestampLocal",
            "source_end_local": "sleepEndTimestampLocal",
        }.items():
            self.field(data, obj, name, path, numeric=False)
        begin, finish = self.window(data, obj, "sleepStartTimestampGMT", "sleepEndTimestampGMT")
        day = self.day(obj) or self.local_day(finish)
        stages = [data[k] for k in ("deep_seconds", "light_seconds", "rem_seconds")]
        if all(v is not None for v in stages):
            data["derived_classified_sleep_seconds"] = sum(stages)
            if data["sleep_seconds"] is not None and sum(stages) != data["sleep_seconds"]:
                total = data["sleep_seconds"]
                minute_rounding = (
                    all(value % 60 == 0 for value in stages)
                    and 0 < sum(stages) - total < 60
                    and sum(stages) == math.ceil(total / 60) * 60
                )
                if minute_rounding:
                    code = "sleep_summary_minute_rounding_consistent"
                    data["quality"]["stage_total_precision"] = code
                    self.issue(code, "sleepTimeSeconds", "info")
                else:
                    self.issue("sleep_summary_totals_differ", "sleepTimeSeconds")
        data["stage_timeline_available"] = False
        self.add("sleep", data, day, begin)
        if "dailyNapDTOS" in obj and obj["dailyNapDTOS"] is not None:
            for nap in self.objects(obj["dailyNapDTOS"], "dailyNapDTOS"):
                self.nap(nap, day)
        # Optional intervals are preserved only if present, never synthesized from totals.
        if "sleepLevels" in obj and obj["sleepLevels"] is not None:
            for stage in self.objects(obj["sleepLevels"], "sleepLevels"):
                item = self.data()
                start, end = self.window(item, stage, "startGMT", "endGMT")
                raw = self.field(item, stage, "raw_stage", "activityLevel", numeric=False)
                label = raw.lower() if isinstance(raw, str) else None
                item["stage"] = label if label in {"awake", "light", "deep", "rem"} else "unknown"
                if item["stage"] == "unknown":
                    self.issue("unknown_sleep_stage", "sleepLevels[].activityLevel")
                if start and end and end > start:
                    data["stage_timeline_available"] = True
                    self.add("sleep_stage", item, day, start, f"{day}:{self.iso(start)}")
                else:
                    self.issue("invalid_sleep_stage_interval", "sleepLevels", "error")
        if not data["stage_timeline_available"]:
            self.issue("sleep_stage_timeline_unavailable", "sleepLevels", "info")

    def nap(self, obj: dict[str, Any], parent_day: str | None) -> None:
        data = self.data()
        self.field(data, obj, "duration_seconds", "napTimeSec")
        self.field(data, obj, "nap_source", "napSource", numeric=False)
        self.field(data, obj, "start_offset_minutes", "napStartTimeOffset", numeric=False)
        self.field(data, obj, "end_offset_minutes", "napEndTimeOffset", numeric=False)
        begin, finish = self.window(data, obj, "napStartTimestampGMT", "napEndTimestampGMT")
        day = self.day(obj) or parent_day or self.local_day(finish)
        self.add("nap", data, day, begin, self.iso(begin))

    def hrv_summary(self, obj: dict[str, Any], chart: dict[str, Any] | None = None) -> None:
        data = self.data()
        mapping = {
            "night_average_ms": "lastNightAvg",
            "weekly_average_ms": "weeklyAvg",
            "highest_5min_ms": "lastNight5MinHigh",
            "baseline_low_upper_ms": "baseline.lowUpper",
            "baseline_balanced_low_ms": "baseline.balancedLow",
            "baseline_balanced_upper_ms": "baseline.balancedUpper",
        }
        for name, path in mapping.items():
            self.field(data, obj, name, path, positive=True)
        self.field(data, obj, "status", "status", numeric=False)
        self.time_field(data, obj, "created_at", "createTimeStamp")
        if chart:
            self.window(data, chart, "startTimestampGMT", "endTimestampGMT")
        self.add("hrv_daily", data, self.day(obj))

    def hrv(self, obj: dict[str, Any]) -> None:
        summary = obj.get("hrvSummary")
        if isinstance(summary, dict) and summary:
            self.hrv_summary(summary, obj)
        elif summary is not None:
            self.issue("unexpected_shape", "hrvSummary", "error")
        readings = obj.get("hrvReadings")
        if readings is None:
            self.issue("sample_series_unavailable", "hrvReadings", "info")
            return
        day = self.sample_day(self.day(summary) if isinstance(summary, dict) else None)
        for i, row in enumerate(self.objects(readings, "hrvReadings")):
            data = self.data()
            self.field(data, row, "hrv_ms", "hrvValue", positive=True)
            timestamp = self.time_field(data, row, "measured_at", "readingTimeGMT")
            self.field(data, row, "source_time_local", "readingTimeLocal", numeric=False)
            data["source_calendar_date"] = day
            data["source_date"] = day
            data["local_date"] = self.local_day(timestamp)
            data["timezone"] = self.zone.key
            data["aggregation_window_seconds"] = None
            self.add(
                "hrv_sample", data, day, timestamp, self.iso(timestamp) or f"{day}:invalid:{i}"
            )

    def chart(
        self,
        obj: dict[str, Any],
        resource: str,
        kind: str,
        values: str,
        descriptors: str,
        index_field: str,
        key_field: str,
        value_key: str,
        normalized_value: str,
        *,
        series_type: str = "native",
        daily_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = daily_data if daily_data is not None else self.data()
        maps = {
            "heart_rate": {
                "minimum_bpm": "minHeartRate",
                "maximum_bpm": "maxHeartRate",
                "resting_bpm": "restingHeartRate",
                "weekly_resting_bpm": "lastSevenDaysAvgRestingHeartRate",
            },
            "body_battery": {"charged": "charged", "drained": "drained"},
            "spo2": {
                "average_percent": "averageSpO2",
                "lowest_percent": "lowestSpO2",
                "latest_percent": "latestSpO2",
                "sleep_average_percent": "avgSleepSpO2",
                "weekly_average_percent": "lastSevenDaysAvgSpO2",
            },
        }
        day = self.sample_day(
            self.day(obj, "date" if resource == "body_battery" else "calendarDate")
        )
        if daily_data is None:
            for name, path in maps[resource].items():
                self.field(
                    data,
                    obj,
                    name,
                    path,
                    positive=resource in {"heart_rate", "spo2"},
                    maximum=100 if resource == "spo2" else None,
                )
            begin, _ = self.window(data, obj, "startTimestampGMT", "endTimestampGMT")
            data["sample_count"] = 0
            data["valid_sample_count"] = 0
            data["coverage_fraction"] = None
            data["series"] = {}
            self.add(f"{kind}_daily", data, day, begin)
        rows = obj.get(values)
        if rows is None:
            self.issue("sample_series_unavailable", values, "info")
            return data
        if not isinstance(rows, list):
            self.issue("unexpected_shape", values, "error")
            return data
        data["sample_count"] += len(rows)
        data["series"][series_type] = {"sample_count": len(rows), "valid_sample_count": 0}
        if not rows:
            return data
        columns: dict[str, int] = {}
        for descriptor in self.objects(obj.get(descriptors), descriptors):
            index, key = descriptor.get(index_field), descriptor.get(key_field)
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or not isinstance(key, str)
                or key in columns
                or index in columns.values()
            ):
                self.issue("invalid_sample_descriptor", descriptors, "error")
                return data
            columns[key] = index
        if "timestamp" not in columns or value_key not in columns:
            self.issue("required_sample_descriptor_missing", descriptors, "error")
            return data
        for i, row in enumerate(rows):
            if not isinstance(row, list) or len(row) <= max(columns.values()):
                self.issue("invalid_sample_row", values, "error")
                continue
            decoded = {key: row[index] for key, index in columns.items()}
            item = self.data()
            measured = self.time_field(item, decoded, "measured_at", "timestamp")
            value = self.field(
                item,
                decoded,
                normalized_value,
                value_key,
                positive=resource in {"heart_rate", "spo2"},
                maximum=100 if resource in {"spo2", "body_battery"} else None,
            )
            item["source_calendar_date"] = day
            item["source_date"] = day
            item["local_date"] = self.local_day(measured)
            item["timezone"] = self.zone.key
            item["source_columns"] = columns.copy()
            item["source_array"] = values
            item["series_type"] = series_type
            item["aggregation_window_seconds"] = 3600 if series_type == "hourly_average" else None
            if resource == "body_battery":
                self.field(item, decoded, "status", "bodyBatteryStatus", numeric=False)
                self.field(item, decoded, "version", "bodyBatteryVersion", numeric=False)
            if resource == "spo2" and series_type == "native":
                self.field(
                    item, decoded, "source_plottable", "singleReadingPlottable", numeric=False
                )
                if item["source_plottable"] is False:
                    item["quality"][normalized_value] = "source_not_plottable"
            if measured and value is not None and item.get("source_plottable") is not False:
                data["valid_sample_count"] += 1
                data["series"][series_type]["valid_sample_count"] += 1
            self.add(
                f"{kind}_sample",
                item,
                day,
                measured,
                f"{series_type}:{self.iso(measured) or f'{day}:invalid:{i}'}",
            )
        return data

    def simple_daily(self, obj: dict[str, Any], kind: str, mapping: dict[str, str]) -> None:
        data = self.data()
        self.fields(data, obj, mapping)
        self.add(kind, data, self.day(obj))

    def daily(self, obj: dict[str, Any], *, legacy: bool) -> None:
        data = self.data()
        self.field(data, obj, "data_source", "source", numeric=False)
        if legacy:
            mapping = {
                "steps": "totalSteps",
                "distance_meters": "totalDistanceMeters",
                "active_seconds": "activeSeconds",
                "highly_active_seconds": "highlyActiveSeconds",
                "sedentary_seconds": "sedentarySeconds",
                "sleeping_seconds": "sleepingSeconds",
                "moderate_minutes": "moderateIntensityMinutes",
                "vigorous_minutes": "vigorousIntensityMinutes",
                "resting_heart_rate": "restingHeartRate",
                "average_stress": "averageStressLevel",
                "stress_seconds": "stressDuration",
                "rest_stress_seconds": "restStressDuration",
                "activity_stress_seconds": "activityStressDuration",
                "uncategorized_stress_seconds": "uncategorizedStressDuration",
                "body_battery_during_sleep": "bodyBatteryDuringSleep",
                "body_battery_at_wake": "bodyBatteryAtWakeTime",
                "body_battery_charged": "bodyBatteryChargedValue",
                "body_battery_drained": "bodyBatteryDrainedValue",
                "average_waking_respiration": "avgWakingRespirationValue",
                "average_spo2": "averageSpo2",
            }
            start, end = "wellnessStartTimeGmt", "wellnessEndTimeGmt"
            duration = "durationInMilliseconds"
            self.time_field(data, obj, "last_sync_at", "lastSyncTimestampGMT")
        else:
            mapping = {
                "steps": "movement.steps.value",
                "distance_meters": "movement.steps.distanceInMeters",
                "moderate_minutes": "intensityMinutes.moderate",
                "vigorous_minutes": "intensityMinutes.vigorous",
                "resting_heart_rate_or_minimum": "heartRate.restingValue",
                "minimum_heart_rate": "heartRate.minValue",
                "maximum_heart_rate": "heartRate.maxValue",
                "average_stress": "stress.avgLevel",
                "maximum_stress": "stress.maxLevel",
                "body_battery_charged": "bodyBattery.chargedValue",
                "body_battery_drained": "bodyBattery.drainedValue",
                "body_battery_latest": "bodyBattery.latestValue",
                "average_respiration": "respiration.avgValue",
                "average_spo2": "pulseOx.avgValue",
                "minimum_spo2": "pulseOx.minValue",
            }
            start, end = (
                "wellnessChronology.startTimestampGmt",
                "wellnessChronology.endTimestampGmt",
            )
            duration = "wellnessChronology.totalDurationInMillis"
            for name, path in {
                "total_stress_seconds": "stress.totalDurationInMillis",
                "rest_stress_seconds": "stress.restDurationInMillis",
                "activity_stress_seconds": "stress.activityDurationInMillis",
                "uncategorized_stress_seconds": "stress.uncategorizedDurationInMillis",
                "low_stress_seconds": "stress.lowStressDurationInMillis",
                "medium_stress_seconds": "stress.mediumStressDurationInMillis",
                "high_stress_seconds": "stress.highStressDurationInMillis",
            }.items():
                self.field(data, obj, name, path, divisor=1000)
        self.fields(data, obj, mapping)
        self.field(data, obj, "wellness_duration_seconds", duration, divisor=1000)
        for name, path in {
            "includes_wellness_data": "includesWellnessData",
            "includes_activity_data": "includesActivityData",
        }.items():
            self.field(data, obj, name, path, numeric=False)
        begin, _ = self.window(data, obj, start, end)
        self.add("daily_summary_legacy" if legacy else "daily_summary", data, self.day(obj), begin)

    def activity(self, obj: dict[str, Any]) -> None:
        data = self.data()
        activity_id = self.field(data, obj, "activity_id", "activityId", numeric=False)
        self.field(data, obj, "sport_type", "activityType.typeKey", numeric=False)
        self.fields(
            data,
            obj,
            {
                "duration_seconds": "duration",
                "elapsed_seconds": "elapsedDuration",
                "moving_seconds": "movingDuration",
                "distance_meters": "distance",
                "training_load": "activityTrainingLoad",
                "average_heart_rate": "averageHR",
                "maximum_heart_rate": "maxHR",
                "aerobic_training_effect": "aerobicTrainingEffect",
                "anaerobic_training_effect": "anaerobicTrainingEffect",
                "moderate_minutes": "moderateIntensityMinutes",
                "vigorous_minutes": "vigorousIntensityMinutes",
                **{f"hr_zone_{n}_seconds": f"hrTimeInZone_{n}" for n in range(1, 6)},
            },
        )
        for name, path in {
            "parent_id": "parentId",
            "is_parent": "isParent",
            "is_manual": "isManualActivity",
            "source_start_local": "startTimeLocal",
        }.items():
            self.field(data, obj, name, path, numeric=False)
        start_path = "beginTimestamp" if obj.get("beginTimestamp") is not None else "startTimeGMT"
        begin = self.time_field(data, obj, "start_at", start_path)
        if begin is None and start_path != "startTimeGMT":
            begin = self.time_field(data, obj, "start_at", "startTimeGMT")
        finish = self.time_field(data, obj, "end_at", "endTimeGMT")
        data["end_at_derived"] = False
        if finish is None and begin and data["elapsed_seconds"] is not None:
            try:
                finish = begin + timedelta(seconds=data["elapsed_seconds"])
                data["end_at"] = self.iso(finish)
                data["end_at_derived"] = True
                data["source"]["end_at"] = "beginTimestamp/startTimeGMT + elapsedDuration"
                data["quality"].pop("end_at", None)
            except OverflowError:
                self.issue("invalid_elapsed_duration", "elapsedDuration")
        if begin and finish and finish < begin:
            data["quality"]["time_window"] = "negative_time_window"
            self.issue("negative_time_window", "endTimeGMT")
        if (
            activity_id is None
            or isinstance(activity_id, bool)
            or not isinstance(activity_id, (str, int))
            or activity_id == ""
        ):
            self.issue("invalid_activity_id", "activityId", "error")
            return
        # Garmin filters activity requests by the activity's own local start day.
        # This offset-free wall time identifies that day, not a source timezone.
        source_day = None
        local_start = data["source_start_local"]
        if (
            isinstance(local_start, str)
            and len(local_start) >= 19
            and local_start[10] in {" ", "T"}
        ):
            try:
                source_day = datetime.fromisoformat(local_start).date().isoformat()
            except ValueError:
                pass
        configured_day = self.local_day(begin)
        data["source_date"] = source_day
        data["source_calendar_date"] = source_day
        data["local_date"] = configured_day
        data["timezone"] = self.zone.key
        data["source"]["source_date"] = "startTimeLocal.date"
        data["source"]["source_calendar_date"] = "startTimeLocal.date"
        data["source"]["local_date"] = "start_at converted to configured timezone"
        if source_day is not None:
            activity_day = source_day
            data["date_basis"] = "source_local_start"
            if configured_day is not None and source_day != configured_day:
                data["quality"]["date"] = "source_date_differs_from_configured_date"
                self.issue("activity_source_date_differs_from_configured_date", "startTimeLocal")
        else:
            activity_day = configured_day
            data["date_basis"] = "configured_timezone_fallback"
            data["quality"]["date"] = "configured_timezone_fallback"
            self.issue("activity_source_date_unavailable", "startTimeLocal")
            if activity_day is None:
                self.issue("activity_date_unavailable", "startTimeLocal", "error")
                return
        self.add(
            "activity",
            data,
            activity_day,
            begin,
            str(activity_id) if activity_id is not None else None,
        )

    def wellness(self, obj: dict[str, Any]) -> None:
        data = self.data()
        identity = self.field(data, obj, "activity_id", "activityUuid.baseUUID", numeric=False)
        self.field(data, obj, "activity_type", "wellnessActivityType", numeric=False)
        begin, _ = self.window(data, obj, "startTimestampGMT", "endTimestampGMT")
        summaries = obj.get("summaryTypeDataList")
        data["summaries"] = []
        if summaries is not None:
            for summary in self.objects(summaries, "summaryTypeDataList"):
                item = self.data()
                self.field(item, summary, "metric", "summaryType", numeric=False)
                self.fields(
                    item,
                    summary,
                    {"minimum": "minValue", "maximum": "maxValue", "average": "avgValue"},
                )
                data["summaries"].append(item)
        self.add("wellness_activity", data, self.day(obj), begin, identity or self.iso(begin))

    def dispatch(self, payload: JsonValue) -> None:
        resource = self.request.resource
        if resource == "activities_page":
            self.batch.metrics["payload_state"] = "raw_only"
            return
        if payload is None:
            self.batch.metrics["payload_state"] = "empty"
            self.issue("empty_payload", "payload", "info")
            return
        if payload == {}:
            self.batch.metrics["payload_state"] = "empty_object"
            self.issue("empty_object", "payload", "error")
            return
        self.batch.metrics["payload_state"] = "present"
        lists = {
            "sleep_summaries",
            "stress",
            "steps",
            "body_battery",
            "wellness_activities",
            "activities",
        }
        if resource == "activities" and isinstance(payload, dict):
            if payload.get("pagination_complete") is not True:
                self.batch.metrics["pagination_complete"] = False
                self.issue("incomplete_activity_pagination", "pagination_complete", "error")
                return
            pages = payload.get("pages")
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                self.issue("unexpected_shape", "pages", "error")
                return
            payload = [item for page in pages for item in page]
            self.batch.metrics["pagination_complete"] = True
        if resource in lists:
            items = self.objects(payload, resource)
            if payload == []:
                self.batch.metrics["payload_state"] = "empty_list"
            if resource == "activities":
                unique: dict[str, dict[str, Any]] = {}
                unidentified = []
                for item in items:
                    identity = item.get("activityId")
                    if identity is None:
                        unidentified.append(item)
                    else:
                        key = str(identity)
                        if key in unique:
                            self.issue("duplicate_activity_id", "activityId", "info")
                        unique[key] = item
                items = list(unique.values()) + unidentified
        elif resource == "hrv_summaries":
            if not isinstance(payload, dict):
                self.issue("unexpected_shape", "hrvSummaries", "error")
                return
            items = self.objects(payload.get("hrvSummaries"), "hrvSummaries")
        elif resource == "daily_summaries":
            if not isinstance(payload, dict):
                self.issue("unexpected_shape", "data", "error")
                return
            items = self.objects(payload.get("data"), "data")
        elif isinstance(payload, dict):
            items = [payload]
        else:
            self.issue("unexpected_shape", "payload", "error")
            return
        for obj in items:
            expected_markers = {
                "sleep": {"calendarDate", "sleepTimeSeconds"},
                "sleep_summaries": {"calendarDate", "sleepTimeSeconds"},
                "hrv": {"hrvSummary", "hrvReadings"},
                "hrv_summaries": {"calendarDate", "lastNightAvg"},
                "heart_rate": {"calendarDate", "heartRateValues"},
                "daily": {"calendarDate", "includesWellnessData"},
                "daily_summaries": {"calendarDate", "wellnessChronology"},
                "body_battery": {"date", "bodyBatteryValuesArray"},
                "spo2": {"calendarDate", "spO2SingleValues", "spO2HourlyAverages"},
                "stress": {"calendarDate", "values"},
                "steps": {"calendarDate", "totalSteps"},
                "wellness_activities": {"calendarDate", "activityUuid"},
                "activities": {"activityId", "activityType", "beginTimestamp"},
            }
            markers = expected_markers.get(resource)
            if markers is not None and not markers.intersection(obj):
                self.issue("unexpected_shape", resource, "error")
                continue
            match resource:
                case "sleep" | "sleep_summaries":
                    self.sleep(obj)
                case "hrv":
                    self.hrv(obj)
                case "hrv_summaries":
                    self.hrv_summary(obj)
                case "heart_rate":
                    self.chart(
                        obj,
                        resource,
                        "heart_rate",
                        "heartRateValues",
                        "heartRateValueDescriptors",
                        "index",
                        "key",
                        "heartrate",
                        "bpm",
                    )
                case "body_battery":
                    self.chart(
                        obj,
                        resource,
                        "body_battery",
                        "bodyBatteryValuesArray",
                        "bodyBatteryValueDescriptorDTOList",
                        "bodyBatteryValueDescriptorIndex",
                        "bodyBatteryValueDescriptorKey",
                        "bodyBatteryLevel",
                        "level",
                    )
                case "spo2":
                    daily_data = self.chart(
                        obj,
                        resource,
                        "spo2",
                        "spO2SingleValues",
                        "spO2SingleValuesDescriptorList",
                        "spo2ValueDescriptorIndex",
                        "spo2ValueDescriptorKey",
                        "spo2Reading",
                        "percent",
                    )
                    if obj.get("spO2HourlyAverages") is not None:
                        self.chart(
                            obj,
                            resource,
                            "spo2",
                            "spO2HourlyAverages",
                            "spO2HourlyAveragesDescriptorList",
                            "spo2ValueDescIndex",
                            "spo2ValueDescKey",
                            "spo2Level",
                            "percent",
                            series_type="hourly_average",
                            daily_data=daily_data,
                        )
                case "stress":
                    self.simple_daily(
                        obj,
                        "stress_daily",
                        {
                            "average_stress": "values.overallStressLevel",
                            "high_seconds": "values.highStressDuration",
                            "medium_seconds": "values.mediumStressDuration",
                            "low_seconds": "values.lowStressDuration",
                            "rest_seconds": "values.restStressDuration",
                        },
                    )
                case "steps":
                    self.simple_daily(
                        obj,
                        "steps_daily",
                        {
                            "steps": "totalSteps",
                            "distance_meters": "totalDistance",
                            "step_goal": "stepGoal",
                        },
                    )
                case "daily_summaries" | "daily":
                    self.daily(obj, legacy=resource == "daily")
                case "activities":
                    self.activity(obj)
                case "wellness_activities":
                    self.wellness(obj)
                case _:
                    self.issue("unsupported_resource", "resource", "error")


def normalize(
    request: DataRequest,
    payload: JsonValue,
    timezone: str = "Asia/Shanghai",
) -> NormalizedBatch:
    """Normalize a response without network, filesystem, or database side effects."""
    try:
        worker = _Normalizer(request, timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return NormalizedBatch(
            issues=[
                {
                    "severity": "error",
                    "code": "invalid_timezone",
                    "field": "timezone",
                }
            ],
            metrics={
                "normalization_version": VERSION,
                "record_count": 0,
                "payload_shape_valid": False,
            },
        )
    worker.dispatch(payload)
    worker.batch.metrics["record_count"] = len(worker.batch.records)
    worker.batch.metrics["kind_counts"] = dict(Counter(r["kind"] for r in worker.batch.records))
    return worker.batch
