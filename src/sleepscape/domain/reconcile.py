"""Independent raw-to-current reconciliation, returning counts only.

This module deliberately does not import or call normalization. It verifies the
selected official source fields directly; unavailable source fields do not count
as matches. Source-internal precision differences are separate from mapping errors.
"""

import math
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from typing import Any

from sleepscape.domain.models import JsonValue

_ABSENT = object()


def _read(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return _ABSENT
        obj = obj[part]
    return obj


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _measurement(value: Any, *, positive: bool = False, divisor: int = 1) -> Any:
    if value is _ABSENT:
        return _ABSENT
    if not _numeric(value) or value < 0 or (positive and value == 0):
        return None
    return value / divisor


def _utc(value: Any, *, normalized: bool = False) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("date")
    try:
        if _numeric(value) and not normalized:
            if not 946684800000 <= value < 4133980800000:
                return None
            return datetime.fromtimestamp(value / 1000, UTC)
        if not isinstance(value, str) or len(value) < 19:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if normalized and (parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0):
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _day(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("localDate")
    try:
        return (
            date.fromisoformat(value).isoformat()
            if isinstance(value, str) and len(value) == 10
            else None
        )
    except ValueError:
        return None


class _Audit:
    def __init__(self, records: list[dict[str, Any]]):
        self.counts = Counter(
            {
                "checked": 0,
                "matches": 0,
                "mismatches": 0,
                "missing_source": 0,
                "missing_records": 0,
                "unexpected_records": 0,
                "source_shape_errors": 0,
            }
        )
        self.records = records
        self.index = defaultdict(list)
        for record in records:
            self.index[(record.get("kind"), record.get("key"))].append(record)
        self.used: set[int] = set()
        self.kinds: set[str] = set()
        self.unverifiable: set[str] = set()

    def compare(self, expected: Any, actual: Any, group: str) -> None:
        if expected is _ABSENT:
            self.counts["missing_source"] += 1
            self.counts[group + "_missing_source"] += 1
            return
        self.counts["checked"] += 1
        self.counts[group + "_checked"] += 1
        if _numeric(expected):
            equal = _numeric(actual) and math.isclose(expected, actual, rel_tol=0, abs_tol=1e-9)
        elif isinstance(expected, bool):
            equal = isinstance(actual, bool) and actual == expected
        else:
            equal = actual is not _ABSENT and actual == expected
        key = "matches" if equal else "mismatches"
        self.counts[key] += 1
        self.counts[group + "_" + key] += 1

    def find(self, kind: str, key: str | None) -> dict[str, Any] | None:
        self.kinds.add(kind)
        if key is None:
            self.counts["missing_source"] += 1
            self.unverifiable.add(kind)
            return None
        candidates = self.index.get((kind, key), [])
        if not candidates:
            self.counts["missing_records"] += 1
            self.compare(True, False, kind + "_record")
            return None
        record = candidates[0]
        self.used.add(id(record))
        return record

    def number(
        self,
        obj: dict,
        record: dict,
        source: str,
        target: str,
        group: str,
        *,
        positive: bool = False,
        divisor: int = 1,
    ) -> None:
        expected = _measurement(_read(obj, source), positive=positive, divisor=divisor)
        self.compare(expected, _read(record, "data." + target), group)

    def time(self, obj: dict, record: dict, source: str, target: str, group: str) -> None:
        raw = _read(obj, source)
        actual = _read(record, target)
        expected = _ABSENT if raw is _ABSENT else _utc(raw)
        converted = _utc(actual, normalized=True)
        if expected is None:
            # An invalid string is not an acceptable normalized null.
            converted = None if actual is None else _ABSENT
        elif actual is _ABSENT:
            converted = _ABSENT
        self.compare(expected, converted, group)

    def objects(self, payload: Any, kind: str) -> list[dict]:
        self.kinds.add(kind)
        if payload is None or payload is _ABSENT:
            self.counts["missing_source"] += 1
            self.unverifiable.add(kind)
            return []
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            self.counts["source_shape_errors"] += 1
            self.unverifiable.add(kind)
            return []
        return payload

    def finish(self) -> dict[str, int]:
        for record in self.records:
            kind = record.get("kind")
            if kind in self.kinds and kind not in self.unverifiable and id(record) not in self.used:
                self.counts["unexpected_records"] += 1
                self.compare(False, True, kind + "_record")
        return dict(self.counts)


def _sleep(audit: _Audit, payload: Any) -> None:
    for obj in audit.objects(payload, "sleep"):
        record = audit.find("sleep", _day(obj.get("calendarDate")))
        if record is None:
            continue
        for source, target in {
            "sleepTimeSeconds": "sleep_seconds",
            "napTimeSeconds": "nap_seconds",
            "deepSleepSeconds": "deep_seconds",
            "lightSleepSeconds": "light_seconds",
            "remSleepSeconds": "rem_seconds",
            "awakeSleepSeconds": "awake_seconds",
            "unmeasurableSleepSeconds": "unmeasurable_seconds",
            "awakeCount": "awake_count",
            "avgSleepStress": "average_stress",
            "averageRespirationValue": "average_respiration",
            "averageSpO2Value": "average_spo2",
            "sleepScores.overall.value": "sleep_score",
        }.items():
            audit.number(obj, record, source, target, "sleep_values")
        audit.number(
            obj, record, "avgHeartRate", "average_heart_rate", "sleep_values", positive=True
        )
        audit.compare(_day(obj.get("calendarDate")), record.get("date"), "sleep_date")
        audit.time(obj, record, "sleepStartTimestampGMT", "data.start_at", "sleep_time")
        audit.time(obj, record, "sleepEndTimestampGMT", "data.end_at", "sleep_time")
        audit.time(obj, record, "sleepStartTimestampGMT", "timestamp", "sleep_time")
        stage_values = [
            obj.get(k) for k in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds")
        ]
        total = obj.get("sleepTimeSeconds")
        if all(_numeric(v) and v >= 0 for v in stage_values + [total]):
            stages = sum(stage_values)
            if stages == total:
                audit.counts["sleep_source_stage_totals_equal"] += 1
            elif (
                all(v % 60 == 0 for v in stage_values)
                and 0 < stages - total < 60
                and stages == math.ceil(total / 60) * 60
            ):
                audit.counts["sleep_source_stage_minute_rounding_consistent"] += 1
            else:
                audit.counts["sleep_source_stage_unexplained_difference"] += 1


def _hrv_summary(audit: _Audit, obj: dict) -> None:
    record = audit.find("hrv_daily", _day(obj.get("calendarDate")))
    if record is None:
        return
    for source, target in {
        "lastNightAvg": "night_average_ms",
        "weeklyAvg": "weekly_average_ms",
        "lastNight5MinHigh": "highest_5min_ms",
    }.items():
        audit.number(obj, record, source, target, "hrv_summary_values", positive=True)
    audit.compare(_day(obj.get("calendarDate")), record.get("date"), "hrv_date")


def _hrv(audit: _Audit, payload: dict) -> None:
    summary = payload.get("hrvSummary")
    if isinstance(summary, dict):
        _hrv_summary(audit, summary)
    else:
        audit.counts["missing_source"] += 1
        audit.unverifiable.add("hrv_daily")
    source_day = _day(summary.get("calendarDate")) if isinstance(summary, dict) else None
    for i, reading in enumerate(audit.objects(payload.get("hrvReadings"), "hrv_sample")):
        instant = _utc(reading.get("readingTimeGMT"))
        key = _iso(instant) or (f"{source_day}:invalid:{i}" if source_day else None)
        record = audit.find("hrv_sample", key)
        if record is None:
            continue
        audit.number(reading, record, "hrvValue", "hrv_ms", "hrv_sample_values", positive=True)
        audit.time(reading, record, "readingTimeGMT", "timestamp", "hrv_sample_time")
        audit.compare(source_day if source_day else _ABSENT, record.get("date"), "hrv_sample_date")


def _activities(audit: _Audit, payload: Any) -> None:
    if isinstance(payload, dict):
        pages = payload.get("pages")
        if payload.get("pagination_complete") is not True or not isinstance(pages, list):
            audit.counts["source_shape_errors"] += 1
            audit.unverifiable.add("activity")
            return
        if any(not isinstance(page, list) for page in pages):
            audit.counts["source_shape_errors"] += 1
            audit.unverifiable.add("activity")
            return
        payload = [item for page in pages for item in page]
    unique = {}
    for obj in audit.objects(payload, "activity"):
        identity = obj.get("activityId")
        if isinstance(identity, bool) or not isinstance(identity, (int, str)) or identity == "":
            audit.counts["missing_source"] += 1
            audit.unverifiable.add("activity")
            continue
        unique[str(identity)] = obj
    for identity, obj in unique.items():
        record = audit.find("activity", identity)
        if record is None:
            continue
        audit.compare(obj["activityId"], _read(record, "data.activity_id"), "activity_id")
        for source, target in {
            "activityTrainingLoad": "training_load",
            "duration": "duration_seconds",
            "elapsedDuration": "elapsed_seconds",
        }.items():
            audit.number(obj, record, source, target, "activity_values")
        path = "beginTimestamp" if _utc(obj.get("beginTimestamp")) else "startTimeGMT"
        audit.time(obj, record, path, "data.start_at", "activity_time")
        audit.time(obj, record, path, "timestamp", "activity_time")
        local = obj.get("startTimeLocal")
        try:
            source_date = (
                datetime.fromisoformat(local).date().isoformat()
                if isinstance(local, str) and len(local) >= 19 and local[10] in {"T", " "}
                else None
            )
        except ValueError:
            source_date = None
        # Configured-timezone fallback dates cannot be verified without that configuration.
        audit.compare(
            source_date if source_date else _ABSENT, record.get("date"), "activity_source_date"
        )


def _daily(audit: _Audit, payload: dict) -> None:
    for obj in audit.objects(payload.get("data"), "daily_summary"):
        record = audit.find("daily_summary", _day(obj.get("calendarDate")))
        if record is None:
            continue
        for source, target in {
            "wellnessChronology.totalDurationInMillis": "wellness_duration_seconds",
            "stress.totalDurationInMillis": "total_stress_seconds",
            "stress.restDurationInMillis": "rest_stress_seconds",
            "stress.activityDurationInMillis": "activity_stress_seconds",
            "stress.uncategorizedDurationInMillis": "uncategorized_stress_seconds",
            "stress.lowStressDurationInMillis": "low_stress_seconds",
            "stress.mediumStressDurationInMillis": "medium_stress_seconds",
            "stress.highStressDurationInMillis": "high_stress_seconds",
        }.items():
            audit.number(obj, record, source, target, "daily_duration_units", divisor=1000)
        for source, target in {
            "movement.steps.value": "steps",
            "stress.avgLevel": "average_stress",
            "intensityMinutes.moderate": "moderate_minutes",
            "intensityMinutes.vigorous": "vigorous_minutes",
        }.items():
            audit.number(obj, record, source, target, "daily_values")
        for source, target in {
            "includesWellnessData": "includes_wellness_data",
            "includesActivityData": "includes_activity_data",
        }.items():
            audit.compare(_read(obj, source), _read(record, "data." + target), "daily_flags")


def _heart_rate(audit: _Audit, payload: dict) -> None:
    source_day = _day(payload.get("calendarDate"))
    daily = audit.find("heart_rate_daily", source_day)
    if daily is not None:
        for source, target in {
            "restingHeartRate": "resting_bpm",
            "minHeartRate": "minimum_bpm",
            "maxHeartRate": "maximum_bpm",
        }.items():
            audit.number(payload, daily, source, target, "heart_rate_daily_values", positive=True)
    rows, descriptors = payload.get("heartRateValues"), payload.get("heartRateValueDescriptors")
    audit.kinds.add("heart_rate_sample")
    if not isinstance(rows, list) or not isinstance(descriptors, list):
        audit.counts["missing_source"] += 1
        audit.unverifiable.add("heart_rate_sample")
        return
    columns = {}
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            break
        key, index = descriptor.get("key"), descriptor.get("index")
        if (
            not isinstance(key, str)
            or not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or key in columns
            or index in columns.values()
        ):
            break
        columns[key] = index
    else:
        if {"timestamp", "heartrate"} <= columns.keys():
            for i, row in enumerate(rows):
                if not isinstance(row, list) or len(row) <= max(columns.values()):
                    audit.counts["source_shape_errors"] += 1
                    audit.unverifiable.add("heart_rate_sample")
                    continue
                raw = {
                    "timestamp": row[columns["timestamp"]],
                    "heartrate": row[columns["heartrate"]],
                }
                instant = _utc(raw["timestamp"])
                key = f"native:{_iso(instant) or f'{source_day}:invalid:{i}'}"
                record = audit.find("heart_rate_sample", key)
                if record is None:
                    continue
                audit.number(
                    raw, record, "heartrate", "bpm", "heart_rate_sample_values", positive=True
                )
                audit.time(raw, record, "timestamp", "timestamp", "heart_rate_sample_time")
                audit.compare(
                    source_day if source_day else _ABSENT,
                    record.get("date"),
                    "heart_rate_sample_date",
                )
            return
    audit.counts["source_shape_errors"] += 1
    audit.unverifiable.add("heart_rate_sample")


def reconcile(resource: str, payload: JsonValue, records: list[dict[str, Any]]) -> dict[str, int]:
    """Audit supported raw fields without exposing values, identifiers, or dates."""
    audit = _Audit(records)
    if payload is None:
        audit.counts["missing_source"] += 1
        return dict(audit.counts)
    match resource:
        case "sleep":
            _sleep(audit, [payload])
        case "sleep_summaries":
            _sleep(audit, payload)
        case "activities":
            _activities(audit, payload)
        case "hrv" | "hrv_summaries" | "heart_rate" | "daily_summaries":
            if not isinstance(payload, dict):
                audit.counts["source_shape_errors"] += 1
                return dict(audit.counts)
            if resource == "hrv":
                _hrv(audit, payload)
            elif resource == "hrv_summaries":
                for obj in audit.objects(payload.get("hrvSummaries"), "hrv_daily"):
                    _hrv_summary(audit, obj)
            elif resource == "heart_rate":
                _heart_rate(audit, payload)
            else:
                _daily(audit, payload)
        case _:
            audit.counts["unsupported_resource"] += 1
    return audit.finish()
