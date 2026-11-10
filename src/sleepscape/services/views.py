"""Browser-safe, read-only views of stored observations, not sleep analysis."""

import hashlib
import math
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sleepscape.config import Settings
from sleepscape.domain.downsample import minmax_downsample
from sleepscape.domain.training_load import load_comparison
from sleepscape.storage.reader import SUCCESS, DashboardReader, validate_dates, validate_window
from sleepscape.storage.reports import read_private_json

RESOURCE_LABELS = {
    "sleep": "睡眠详情",
    "sleep_summaries": "睡眠摘要",
    "hrv": "夜间 HRV",
    "hrv_summaries": "HRV 摘要",
    "heart_rate": "心率",
    "daily": "每日摘要",
    "daily_summaries": "每日摘要",
    "stress": "压力",
    "steps": "步数",
    "body_battery": "身体电量",
    "spo2": "血氧",
    "activities": "活动",
    "activities_page": "活动分页",
    "wellness_activities": "健康活动",
}
_CODES = set(
    """
missing invalid_number negative_or_sentinel nonpositive_measurement outside_scale
source_not_plottable
invalid_timestamp_unit_or_range invalid_timestamp invalid_calendar_date
record_without_calendar_date conflicting_duplicate_record unexpected_shape invalid_list_item
sleep_summary_minute_rounding_consistent sleep_summary_totals_differ
sleep_stage_timeline_unavailable invalid_sleep_stage_interval unknown_sleep_stage
sample_series_unavailable invalid_sample_descriptor required_sample_descriptor_missing
invalid_sample_row invalid_elapsed_duration negative_time_window invalid_activity_id
source_date_differs_from_configured_date configured_timezone_fallback
activity_source_date_differs_from_configured_date activity_source_date_unavailable
activity_date_unavailable incomplete_activity_pagination duplicate_activity_id
unsupported_resource invalid_timezone empty_payload empty_object invalid_payload
authentication_retry_exhausted authentication_network_retry_exhausted
authentication_network_error credential_file_unavailable authentication_invalid_response
retry_after_deferred authorization_failed http_error retry_exhausted invalid_json_response
network_retry_exhausted invalid_api_response collection_error local_collection_error
normalization_error pagination_incomplete pagination_limit_exceeded pagination_no_progress
request_not_allowed response_too_large invalid_response_size
request_outside_allowed_scope redirect_blocked credential_symlink_not_allowed
credential_file_permissions credential_directory_permissions empty_key_file
authentication_rejected expired_token activity_page_invalid_shape activity_pagination_no_progress
activity_page_invalid_id activity_pagination_overlap activity_pagination_limit
sync_state_unavailable collection_already_running retry_after_wait_still_active
""".split()
)
_SOURCE_FIELDS = {
    "sleep_seconds": "sleepTimeSeconds",
    "average_heart_rate": "avgHeartRate",
    "night_average_ms": "lastNightAvg",
    "weekly_average_ms": "weeklyAvg",
    "average_respiration": "averageRespirationValue",
    "sleep_score": "sleepScores.overall.value",
    "body_battery_at_wake": "bodyBatteryAtWakeTime",
    "training_load": "activityTrainingLoad",
    "deep_seconds": "deepSleepSeconds",
    "light_seconds": "lightSleepSeconds",
    "rem_seconds": "remSleepSeconds",
    "awake_seconds": "awakeSleepSeconds",
    "unmeasurable_seconds": "unmeasurableSleepSeconds",
}
_SERIES = {
    "heart_rate_sample": ("heart_rate", "心率", "bpm", "bpm", 60),
    "hrv_sample": ("hrv", "HRV", "ms", "hrv_ms", 150),
    "spo2_sample": ("spo2", "血氧", "%", "percent", 120),
    "body_battery_sample": ("body_battery", "身体电量", "points", "level", 150),
}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        value = datetime.fromisoformat(value)
    except ValueError:
        return None
    return value.astimezone(UTC) if value.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _date(value) -> str | None:
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) else None
    except ValueError:
        return None


def _codes(values) -> list[str]:
    return sorted({value for value in values if isinstance(value, str) and value in _CODES})


def _quality(row, field=None) -> list[str]:
    quality = row.get("data", {}).get("quality", {}) if row else {}
    if not isinstance(quality, dict):
        return []
    values = (
        quality.values()
        if field is None
        else (quality.get(key) for key in (field, "date", "time_window", "stage_total_precision"))
    )
    return [code for code in _codes(values) if code != "missing"]


def _field(resource, field):
    if field == "average_stress":
        return {
            "daily_summaries": "stress.avgLevel",
            "daily": "averageStressLevel",
            "stress": "values.overallStressLevel",
        }.get(resource, field)
    if field == "steps":
        return "movement.steps.value" if resource == "daily_summaries" else "totalSteps"
    return _SOURCE_FIELDS.get(field, field)


def _metric(
    value=None,
    unit=None,
    state="missing",
    resource=None,
    field=None,
    source_date=None,
    fetched_at=None,
    reason="not_collected",
    quality=None,
):
    return {
        "value": value,
        "unit": unit,
        "state": state,
        "source_resource": resource,
        "source_field": field,
        "source_date": source_date,
        "fetched_at": fetched_at,
        "reason": reason,
        "quality": quality or [],
    }


class _Context:
    def __init__(self, reader, start, end, today):
        self.rows = reader.summaries(start, end)
        self.today = today
        self.by_day = defaultdict(list)
        for row in self.rows:
            self.by_day[row["date"]].append(row)
        self.latest, self.success = {}, {}
        self.add_attempts(reader.attempts(start, end), start, end)

    def add_attempts(self, attempts, start, end):
        for attempt in attempts:
            begin = max(start, date.fromisoformat(attempt["start_date"]))
            finish = min(end, date.fromisoformat(attempt["end_date"]))
            for offset in range((finish - begin).days + 1):
                key = (attempt["resource"], (begin + timedelta(days=offset)).isoformat())
                self.latest[key] = attempt
                if attempt["status"] in SUCCESS:
                    self.success[key] = attempt

    def select(self, day, kind, resources, field=None):
        rows = self.by_day[day]
        candidates = [
            row
            for resource in resources
            for row in reversed(rows)
            if row["kind"] == kind and row["resource"] == resource
        ]
        if field:
            found = next((row for row in candidates if row["data"].get(field) is not None), None)
            if found:
                return found
        return candidates[0] if candidates else None

    def stale(self, resource, day):
        return self.latest.get((resource, day), {}).get("status") in {"failed", "invalid"}

    def metric(self, row, field, unit, day):
        resource = row["resource"] if row else None
        raw = row["data"].get(field) if row else None
        value = raw if _number(raw) and raw >= 0 else None
        quality = _quality(row, field)
        state, reason = "available", None
        if value is None:
            field_quality = row["data"].get("quality", {}).get(field) if row else None
            invalid = raw is not None or field_quality not in {None, "missing", "null"}
            state, reason = (
                ("invalid", "invalid_source_value")
                if invalid
                else ("missing", "source_field_missing" if row else "not_collected")
            )
        if row and self.stale(resource, row["date"]):
            state, reason = (
                ("stale", "latest_request_failed")
                if value is not None
                else (state, "latest_request_failed")
            )
            quality = sorted(set(quality + ["latest_request_failed"]))
        elif day >= self.today.isoformat():
            state, reason = "pending", "current_day_provisional"
        return _metric(
            value,
            unit,
            state,
            resource,
            _field(resource, field),
            row["date"] if row else day,
            row["fetched_at"] if row else None,
            reason,
            quality,
        )

    def day_load(self, day: date):
        value = day.isoformat()
        rows = [
            row
            for row in self.by_day[value]
            if row["kind"] == "activity" and row["resource"] == "activities"
        ]
        success = self.success.get(("activities", value))
        complete = success and success["metrics"].get("pagination_complete") is True
        loads = [row["data"].get("training_load") for row in rows]
        valid = [load for load in loads if _number(load) and load >= 0]
        quality = sorted({code for row in rows for code in _quality(row)})
        total, reason = None, "activity_scope_incomplete"
        stale = self.stale("activities", value)
        if complete:
            if rows and len(loads) == len(valid):
                total, reason = sum(valid), None
            elif rows:
                reason = "partial_activity_load"
            else:
                daily = self.select(value, "daily_summary", ("daily_summaries",))
                daily_success = self.success.get(("daily_summaries", value))
                if daily and daily_success:
                    total, reason = 0, "no_recorded_activity"
                    stale = stale or self.stale("daily_summaries", value)
                else:
                    reason = "daily_summary_missing"
        state = "available" if total is not None else "missing"
        if total is not None and stale:
            state, reason = "stale", "latest_request_failed"
        elif day >= self.today:
            state, reason = "pending", "current_day_provisional"
        fetched = max((row["fetched_at"] for row in rows), default=None)
        result = _metric(
            total,
            "load",
            state,
            "activities",
            "activityTrainingLoad",
            value,
            fetched or (success["fetched_at"] if success else None),
            reason,
            quality,
        )
        result.update(activity_count=len(rows), known_load_sum=sum(valid))
        return result


def _comparison(context, day):
    target = day - timedelta(days=1)
    baseline_start = day - timedelta(days=31)
    current = context.day_load(target)
    baseline = [context.day_load(baseline_start + timedelta(days=i)) for i in range(30)]
    comparison = load_comparison(
        [{"date": item["source_date"], "load": item["value"]} for item in [current, *baseline]],
        target.isoformat(),
    )
    state, reason = "available", None
    if current["value"] is None:
        state, reason = "missing", "target_load_missing"
    elif comparison["baseline_valid_days"] < 30:
        state, reason = "missing", "baseline_incomplete"
    elif comparison["baseline_mean"] == 0:
        state, reason = "unavailable", "baseline_zero"
    if any(item["state"] == "stale" for item in [current, *baseline]):
        state, reason = "stale", "latest_request_failed"
    elif any(item["state"] == "pending" for item in [current, *baseline]):
        state, reason = "pending", "current_day_provisional"
    return current, {
        **{
            key: comparison[key]
            for key in (
                "target_date",
                "target_load",
                "baseline_start",
                "baseline_end",
                "baseline_valid_days",
                "baseline_required_days",
                "baseline_mean",
                "change_percent",
            )
        },
        "baseline_fresh_days": sum(item["state"] == "available" for item in baseline),
        "state": state,
        "reason": reason,
        "quality": sorted({code for item in [current, *baseline] for code in item["quality"]}),
    }


def _event(row, context):
    data = row["data"]
    start, end = _time(data.get("start_at")), _time(data.get("end_at"))
    if start is None:
        return None
    quality = _quality(row)
    if data.get("end_at_derived") is True:
        quality.append("end_at_derived")
    if end and end < start:
        end = None
        quality.append("negative_time_window")
    state = "stale" if context.stale(row["resource"], row["date"]) else "available"
    if state == "stale":
        quality.append("latest_request_failed")
    label = (
        "午睡"
        if row["kind"] == "nap"
        else {
            "running": "跑步",
            "cycling": "骑行",
            "walking": "步行",
            "swimming": "游泳",
            "strength_training": "力量训练",
            "hiking": "徒步",
        }.get(str(data.get("sport_type", "")).lower(), "活动")
    )
    identity = f"{row['resource']}:{row['kind']}:{row['key']}"
    return {
        "key": hashlib.sha256(identity.encode()).hexdigest()[:24],
        "type": row["kind"],
        "label": label,
        "start_at": _iso(start),
        "end_at": _iso(end) if end else None,
        "source_date": _date(row["date"]),
        "metrics": {
            field: data.get(field) if _number(data.get(field)) else None
            for field in ("duration_seconds", "training_load", "average_heart_rate")
        },
        "quality": sorted(set(quality)),
        "state": state,
    }


class DashboardService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.zone = ZoneInfo(settings.timezone)

    def _now(self):
        return datetime.now(UTC)

    def _backup_status(self):
        """Read only bounded local verification metadata, never a backup path or contents."""
        path = self.settings.data_dir / "reports" / "backup-status.json"
        try:
            metadata = read_private_json(path)
            timestamp = _time(metadata.get("last_success_at"))
            if timestamp is not None and metadata.get("status") == "verified":
                return {"state": "verified", "last_success_at": _iso(timestamp)}
        except FileNotFoundError:
            return {"state": "not_configured", "last_success_at": None}
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        return {"state": "unknown", "last_success_at": None}

    def _header(self, reader, quality=None):
        return {
            "contract_version": "1",
            "timezone": self.settings.timezone,
            "generated_at": _iso(self._now()),
            "data_as_of": reader.data_as_of(),
            "quality": quality or [],
        }

    def _night(self, context, day):
        value = day.isoformat()
        sleep = context.select(value, "sleep", ("sleep", "sleep_summaries"))
        hrv = context.select(value, "hrv_daily", ("hrv", "hrv_summaries"))
        previous = (day - timedelta(days=1)).isoformat()
        daily = context.select(previous, "daily_summary", ("daily_summaries",))
        legacy = context.select(previous, "daily_summary_legacy", ("daily",))
        stress = context.select(previous, "stress_daily", ("stress",))
        steps = context.select(previous, "steps_daily", ("steps",))
        wake = context.select(value, "daily_summary_legacy", ("daily",))
        load, comparison = _comparison(context, day)

        def previous_metric(field, unit, fallback):
            candidates = [daily, legacy, fallback]
            row = next(
                (row for row in candidates if row and row["data"].get(field) is not None),
                next((row for row in candidates if row), None),
            )
            return context.metric(row, field, unit, previous)

        cards = {
            "training_load": load,
            "average_heart_rate": context.metric(sleep, "average_heart_rate", "bpm", value),
            "night_average_hrv": context.metric(hrv, "night_average_ms", "ms", value),
            "sleep_seconds": context.metric(sleep, "sleep_seconds", "s", value),
            "previous_stress": previous_metric("average_stress", "points", stress),
            "previous_steps": previous_metric("steps", "steps", steps),
            "average_respiration": context.metric(
                sleep, "average_respiration", "breaths/min", value
            ),
            "sleep_score": context.metric(sleep, "sleep_score", "points", value),
            "weekly_average_hrv": context.metric(hrv, "weekly_average_ms", "ms", value),
            "body_battery_at_wake": context.metric(wake, "body_battery_at_wake", "points", value),
        }
        data = sleep["data"] if sleep else {}
        start, end = _time(data.get("start_at")), _time(data.get("end_at"))
        quality = _quality(sleep)
        if sleep and sleep["resource"] != "sleep":
            quality.append("sleep_summary_fallback")
        if not data.get("stage_timeline_available"):
            quality.append("sleep_stage_timeline_unavailable")
        if (
            any(metric["state"] == "stale" for metric in cards.values())
            or comparison["state"] == "stale"
        ):
            quality.append("latest_request_failed")
        if day >= context.today:
            quality.append("current_day_provisional")
        stages = []
        for stage, label in (
            ("deep", "深睡"),
            ("light", "浅睡"),
            ("rem", "快速眼动"),
            ("awake", "清醒"),
            ("unmeasurable", "未分类"),
        ):
            metric = context.metric(sleep, stage + "_seconds", "s", value)
            stages.append(
                {
                    "stage": stage,
                    "label": label,
                    "seconds": metric["value"],
                    "state": metric["state"],
                }
            )
        activities = [
            event
            for row in context.by_day[previous]
            if row["kind"] == "activity" and row["resource"] == "activities"
            if (event := _event(row, context))
        ]
        return {
            "date": value,
            "is_provisional": day >= context.today,
            "sleep": {
                "start_at": _iso(start) if start else None,
                "end_at": _iso(end) if end else None,
                "stage_timeline_available": bool(data.get("stage_timeline_available")),
            },
            "cards": cards,
            "load_comparison": comparison,
            "stages": stages,
            "activities": activities,
            "quality": sorted(set(quality)),
        }

    def night(self, day: date) -> dict | None:
        with DashboardReader(self.settings.data_dir) as reader:
            context = _Context(
                reader, day - timedelta(days=31), day, self._now().astimezone(self.zone).date()
            )
            if not context.by_day[day.isoformat()]:
                return None
            return {**self._header(reader), **self._night(context, day)}

    def nights(self, start: date, end: date, cursor: date | None = None, limit: int = 31) -> dict:
        validate_dates(start, end, 92)
        if isinstance(limit, bool) or not 1 <= limit <= 92:
            raise ValueError("invalid_limit")
        if cursor is not None and not start <= cursor <= end:
            raise ValueError("invalid_cursor")
        last = cursor or end
        days = [last - timedelta(days=i) for i in range(min(limit, (last - start).days + 1))]
        with DashboardReader(self.settings.data_dir) as reader:
            context = _Context(
                reader,
                min(days) - timedelta(days=31),
                last,
                self._now().astimezone(self.zone).date(),
            )
            items = []
            for day in days:
                night = self._night(context, day)
                comparison = night["load_comparison"]
                change = _metric(
                    comparison["change_percent"],
                    "%",
                    comparison["state"],
                    "activities",
                    "activityTrainingLoad",
                    comparison["target_date"],
                    night["cards"]["training_load"]["fetched_at"],
                    comparison["reason"],
                    comparison["quality"],
                )
                items.append(
                    {
                        "date": day.isoformat(),
                        **{
                            name: night["cards"][name]
                            for name in (
                                "sleep_seconds",
                                "average_heart_rate",
                                "night_average_hrv",
                                "training_load",
                            )
                        },
                        "change_percent": change,
                        "quality": night["quality"],
                    }
                )
            next_day = min(days) - timedelta(days=1)
            return {
                **self._header(reader),
                "items": items,
                "next_cursor": next_day.isoformat() if next_day >= start else None,
                "available_dates": reader.available_dates(start, end),
            }

    def timeline(
        self,
        day: date,
        start: datetime | None = None,
        end: datetime | None = None,
        max_points: int = 2000,
    ) -> dict | None:
        if isinstance(max_points, bool) or not 2 <= max_points <= 2000:
            raise ValueError("invalid_max_points")
        if any(value is not None and value.tzinfo is None for value in (start, end)):
            raise ValueError("timezone_required")
        if start is not None and end is not None:
            validate_window(start, end)
        with DashboardReader(self.settings.data_dir) as reader:
            today = self._now().astimezone(self.zone).date()
            context = _Context(reader, day - timedelta(days=1), day, today)
            if not context.by_day[day.isoformat()]:
                return None
            sleep = context.select(day.isoformat(), "sleep", ("sleep", "sleep_summaries"))
            data = sleep["data"] if sleep else {}
            begin, finish = _time(data.get("start_at")), _time(data.get("end_at"))
            quality = []
            if begin and finish and begin < finish and finish - begin <= timedelta(hours=46):
                default_start, default_end = begin - timedelta(hours=1), finish + timedelta(hours=1)
            else:
                default_start = datetime.combine(day - timedelta(days=1), time(18), self.zone)
                default_end = datetime.combine(day, time(12), self.zone)
                quality.append("sleep_window_unavailable")
            start, end = start or default_start, end or default_end
            validate_window(start, end)
            samples, events = reader.samples(start, end), reader.events(start, end)
            partitions = [
                date.fromisoformat(row["date"]) for row in [*samples, *events] if _date(row["date"])
            ]
            if partitions:
                first, last = min(partitions), max(partitions)
                # Unusual travel/source partitions must not turn a 48h query into unbounded reads.
                if (last - first).days < 123:
                    context.add_attempts(reader.attempts(first, last), first, last)
                else:
                    quality.append("source_partition_range_unavailable")
            grouped = defaultdict(list)
            for row in samples:
                if row["kind"] in _SERIES:
                    series_type = row["data"].get("series_type", "native")
                    if series_type in {"native", "hourly_average"}:
                        grouped[(row["kind"], series_type)].append(row)
            series = []
            for (kind, series_type), rows in sorted(grouped.items()):
                metric, label, unit, field, tolerance = _SERIES[kind]
                points, stale = [], False
                for row in rows:
                    item = context.metric(row, field, unit, row["date"])
                    if row["data"].get("source_plottable") is False:
                        item["value"] = None
                    stale = stale or item["state"] == "stale"
                    points.append(
                        {
                            "timestamp": row["timestamp"],
                            "value": item["value"],
                            "quality": item["quality"],
                            "source_date": _date(row["date"]),
                            "fetched_at": row["fetched_at"],
                        }
                    )
                reduced = minmax_downsample(points, start, end, max_points)
                state = "available" if reduced["points"] else "missing"
                if stale:
                    state = "stale"
                elif day >= today:
                    state = "pending"
                series.append(
                    {
                        "id": f"{metric}:{series_type}",
                        "metric": metric,
                        "label": label
                        + ("（小时均值）" if series_type == "hourly_average" else ""),
                        "unit": unit,
                        "series_type": series_type,
                        "aggregation_window_seconds": 3600
                        if series_type == "hourly_average"
                        else None,
                        "hover_tolerance_seconds": 1800
                        if series_type == "hourly_average"
                        else tolerance,
                        "state": state,
                        **reduced,
                    }
                )
            for metric, label, unit, _, tolerance in _SERIES.values():
                if not any(item["metric"] == metric for item in series):
                    series.append(
                        {
                            "id": f"{metric}:native",
                            "metric": metric,
                            "label": label,
                            "unit": unit,
                            "series_type": "native",
                            "aggregation_window_seconds": None,
                            "hover_tolerance_seconds": tolerance,
                            "state": "pending" if day >= today else "missing",
                            "points": [],
                            "display_resolution": {
                                "method": "none",
                                "original_count": 0,
                                "valid_count": 0,
                                "returned_count": 0,
                                "bucket_seconds": None,
                            },
                        }
                    )
            chosen_events = {}
            intervals = []
            for row in events:
                if row["kind"] == "sleep_stage":
                    if (
                        sleep
                        and row["resource"] == sleep["resource"]
                        and row["date"] == day.isoformat()
                    ):
                        stage = row["data"].get("stage")
                        if stage in {"deep", "light", "rem", "awake", "unknown"}:
                            intervals.append(
                                {
                                    "stage": stage,
                                    "start_at": row["data"].get("start_at"),
                                    "end_at": row["data"].get("end_at"),
                                }
                            )
                    continue
                event = _event(row, context)
                if event:
                    identity = (
                        event["type"],
                        event["start_at"],
                        event["end_at"],
                        event["key"] if event["type"] == "activity" else None,
                    )
                    # Detail and summary endpoints may carry the same nap.
                    if identity not in chosen_events or row["resource"] == "sleep":
                        chosen_events[identity] = event
            return {
                **self._header(reader, quality),
                "date": day.isoformat(),
                "window": {
                    "start": _iso(start),
                    "end": _iso(end),
                    "default_start": _iso(default_start),
                    "default_end": _iso(default_end),
                },
                "series": series,
                "events": list(chosen_events.values()),
                "stages": intervals,
            }

    def status(self) -> dict:
        with DashboardReader(self.settings.data_dir) as reader:
            info = reader.status()
            last_success = _time(info["data_as_of"])
            stale = last_success is None or self._now() - last_success > timedelta(hours=36)
            quality = ["collection_not_recent"] if stale else []
            if not info["present"]:
                quality.append("database_missing")
            run = info["latest_run"]
            latest = None
            if run:
                states = {"running", "completed", "partial", "failed", "interrupted"}
                summary, plan = run["summary"], run["plan"]
                latest = {
                    "status": run["status"] if run["status"] in states else "unknown",
                    "started_at": _iso(_time(run["started_at"]))
                    if _time(run["started_at"])
                    else None,
                    "finished_at": _iso(_time(run["finished_at"]))
                    if _time(run["finished_at"])
                    else None,
                    "end_date": _date(plan.get("end_date")),
                    "detail_start": _date(plan.get("detail_start")),
                    "stop_reason": summary.get("stop_reason")
                    if summary.get("stop_reason") in _CODES
                    else None,
                    "retry_not_before": _iso(_time(summary.get("retry_not_before")))
                    if _time(summary.get("retry_not_before"))
                    else None,
                }
            resources = []
            for item in info["resources"]:
                resource = item["resource"]
                if resource not in RESOURCE_LABELS:
                    continue
                code = item["error_code"]
                resources.append(
                    {
                        "resource": resource,
                        "label": RESOURCE_LABELS[resource],
                        "last_status": item["status"]
                        if item["status"] in SUCCESS | {"failed", "invalid"}
                        else "unknown",
                        "last_attempt_at": item["fetched_at"],
                        "last_success_at": info["last_success"].get(resource),
                        "error_code": code
                        if code in _CODES
                        else ("collection_error" if code else None),
                        "issues": _codes(
                            issue.get("code") for issue in item["issues"] if isinstance(issue, dict)
                        ),
                    }
                )
            has_sleep = info["latest_night"] is not None
            capabilities = [
                {
                    "key": "sleep_summary",
                    "label": "睡眠摘要",
                    "available": has_sleep,
                    "note": "stored_observations" if has_sleep else "not_collected",
                },
                {
                    "key": "stage_timeline",
                    "label": "睡眠分期区间",
                    "available": False,
                    "note": "requires_source_intervals",
                },
                {
                    "key": "environment",
                    "label": "卧室环境",
                    "available": False,
                    "note": "hardware_not_connected",
                },
            ]
            if has_sleep:
                day = date.fromisoformat(info["latest_night"])
                context = _Context(reader, day, day, self._now().astimezone(self.zone).date())
                sleep = context.select(day.isoformat(), "sleep", ("sleep", "sleep_summaries"))
                capabilities[1]["available"] = bool(
                    sleep and sleep["data"].get("stage_timeline_available")
                )
                if capabilities[1]["available"]:
                    capabilities[1]["note"] = "stored_source_intervals"
            return {
                **self._header(reader, quality),
                "latest_night": info["latest_night"],
                "earliest_night": info["earliest_night"],
                "latest_success_at": info["data_as_of"],
                "stale": stale,
                "latest_collection": latest,
                "database": {
                    key: info[key]
                    for key in (
                        "present",
                        "size_bytes",
                        "record_count",
                        "raw_snapshot_count",
                        "request_count",
                    )
                },
                "resources": resources,
                "capabilities": capabilities,
                "backup": self._backup_status(),
            }
