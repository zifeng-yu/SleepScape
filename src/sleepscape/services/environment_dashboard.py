"""Read-only environment projection into the dashboard's shared timeline contract."""

import sqlite3
import time
from collections import defaultdict
from datetime import datetime

from sleepscape.config import Settings
from sleepscape.domain.environment_display import finite, reduce_windows
from sleepscape.domain.environment_time import utc_to_us
from sleepscape.domain.metrics import metric_definition, metric_registry
from sleepscape.services.environment_views import EnvironmentViewService
from sleepscape.storage import event_audio

MAX_GROUPS = 8
MAX_EVENTS = 100
MAX_SOURCES = 32
MAX_GAPS = 128
_METRICS = tuple(
    item["id"] for item in metric_registry()["items"] if item["source"] == "environment"
)
_AUDIO = (
    "expected_samples",
    "observed_samples",
    "valid_samples",
    "valid_duration_ms",
    "valid_1s_count",
    "dma_overflow_count",
    "clipped_samples",
)


def _point(row: dict, metric: str, peak: bool, segment: str) -> dict:
    channel = row["metrics"][metric]
    sound = metric == "sound_relative"
    field = "max_1s_rms_dbfs" if peak else "energy_mean_dbfs" if sound else "mean"
    value = channel[field]
    return {
        "timestamp": row["utc_start"],
        "window_start": row["utc_start"],
        "window_end": row["utc_end"],
        "value": value,
        "min": value if sound else channel["min"],
        "max": value if sound else channel["max"],
        "quality": sorted(set(channel["quality"] + row["quality"] + row["time_flags"])),
        "channel_state": channel["state"],
        "expected_count": channel["expected_count"],
        "observed_count": channel["observed_count"],
        "valid_count": channel["valid_count"],
        "time_quality": row["time_quality"],
        "time_uncertainty_ms": row["time_uncertainty_ms"],
        "received_at": row["received_at"],
        "source_ref": row["source_ref"],
        "boot_ref": row["boot_ref"],
        "record_ref": row["record_ref"],
        "segment_ref": segment,
        "coverage": {name: channel[name] for name in _AUDIO} if sound else None,
        "event_detection": {
            "detector_state": channel["event_detector_state"],
            "valid_duration_ms": channel["event_valid_duration_ms"],
            **{
                name: channel[name]
                for name in ("detected_count", "stored_count", "merged_count", "dropped_count")
            },
        }
        if sound
        else None,
    }


def _summary(rows: list[dict], metric: str) -> dict:
    channels = [row["metrics"][metric] for row in rows]
    valid = [channel for channel in channels if channel["valid_count"] > 0]
    expected = sum(channel["expected_count"] for channel in channels)
    actual = sum(channel["valid_count"] for channel in channels)
    sound = metric == "sound_relative"
    lows = [channel["energy_mean_dbfs" if sound else "min"] for channel in valid]
    highs = [channel["max_1s_rms_dbfs" if sound else "max"] for channel in valid]
    return {
        "metric": metric,
        "source_ref": rows[0]["source_ref"],
        "boot_ref": rows[0]["boot_ref"],
        "window_count": len(rows),
        "valid_window_count": len(valid),
        "partial_window_count": sum(channel["state"] == "partial" for channel in channels),
        "missing_window_count": len(rows) - len(valid),
        "expected_count": expected,
        "observed_count": sum(channel["observed_count"] for channel in channels),
        "valid_count": actual,
        "coverage_percent": actual / expected * 100 if expected else None,
        "min": min((value for value in lows if finite(value)), default=None),
        "max": max((value for value in highs if finite(value)), default=None),
        "quality": sorted({flag for channel in channels for flag in channel["quality"]}),
        "scope": "returned_original_windows",
    }


def _event(row: dict) -> dict:
    event = row["event"]
    return {
        "key": row["record_ref"],
        "type": "environment_sound",
        "label": "相对声音事件",
        "start_at": row["utc_start"],
        "end_at": row["utc_end"],
        "source_ref": row["source_ref"],
        "boot_ref": row["boot_ref"],
        "time_quality": row["time_quality"],
        "time_uncertainty_ms": row["time_uncertainty_ms"],
        "received_at": row["received_at"],
        "state": "partial" if event["incomplete"] else "available",
        "quality": sorted(set(row["quality"] + row["time_flags"])),
        "metrics": {
            name: event[name] for name in ("active_duration_ms", "max_1s_rms_dbfs", "baseline_dbfs")
        },
        **{
            name: event[name]
            for name in ("merged", "incomplete", "continued_from_previous", "continues_next")
        },
        "envelope_is_continuous_sound": False,
    }


def _bounded_coverage(coverage: dict | None) -> dict | None:
    if coverage is None:
        return None
    return {
        **coverage,
        "gaps_truncated": len(coverage["gaps"]) > MAX_GAPS,
        "overlaps_truncated": len(coverage["overlaps"]) > MAX_GAPS,
        "gaps": coverage["gaps"][:MAX_GAPS],
        "overlaps": coverage["overlaps"][:MAX_GAPS],
    }


def environment_status(settings: Settings, now: datetime) -> dict:
    try:
        return EnvironmentViewService(settings).status(now=now)
    except Exception:
        return {
            "state": "unavailable",
            "devices": [],
            "devices_truncated": False,
            "global_time_state": None,
            "checked_at": now.isoformat(),
            "demo": settings.demo,
        }


def environment_timeline(
    settings: Settings, start: datetime, end: datetime, max_points: int
) -> dict:
    try:
        view = EnvironmentViewService(settings).timeline(start, end)
    except Exception:
        return {
            "series": [],
            "events": [],
            "environment": {
                "state": "unavailable",
                "record_count": 0,
                "sources": [],
                "segments": [],
                "channel_summaries": [],
                "coverage": None,
                "global_time_state": None,
                "truncated": False,
                "series_truncated": False,
                "events_truncated": False,
                "analysis_readiness_evaluated": False,
            },
        }
    rows = view.pop("records")
    grouped = defaultdict(list)
    for row in rows:
        if row["record_type"] == "minute":
            grouped[(row["source_ref"], row["boot_ref"])].append(row)
    selected = list(grouped.items())
    if len(selected) > MAX_GROUPS:
        # Keep recent captures when earlier restarts fill the bounded display budget.
        # Receipt order must not let a late upload of old data displace the current night.
        newest = sorted(
            grouped,
            key=lambda key: (-max(utc_to_us(row["utc_end"]) for row in grouped[key]), key),
        )[:MAX_GROUPS]
        selected_keys = set(newest)
        # Preserve the reader's chronological group order and every group's original points.
        selected = [(key, records) for key, records in selected if key in selected_keys]
    series, summaries = [], []
    for (source, boot), records in selected:
        for metric in _METRICS:
            definition = metric_definition(metric)
            summaries.append(_summary(records, metric))
            for peak in [False, True] if metric == "sound_relative" else [False]:
                points, previous, segment = [], None, ""
                for row in records:
                    if previous is None or previous["utc_end"] != row["utc_start"]:
                        segment = row["record_ref"]
                    point = _point(row, metric, peak, segment)
                    points.append(point)
                    previous = row if finite(point["value"]) else None
                series_type = "minute_peak" if peak else "minute_mean"
                series.append(
                    {
                        "id": f"{metric}:{series_type}:{source}:{boot}",
                        "metric": metric,
                        "label": definition["label"] + ("（1秒峰值）" if peak else "（分钟均值）"),
                        "unit": definition["unit"],
                        "source": "environment",
                        "source_ref": source,
                        "boot_ref": boot,
                        "series_type": series_type,
                        "default_visible": definition["default_visible"] and not peak,
                        "aggregation_window_seconds": 60,
                        "hover_tolerance_seconds": 0,
                        "state": "available"
                        if any(finite(p["value"]) for p in points)
                        else "missing",
                        **reduce_windows(points, start, end, max_points),
                    }
                )
    events = [_event(row) for row in rows if row["record_type"] == "event"]
    kept_sources = {key[0] for key, _ in selected}
    for event in events[:MAX_EVENTS]:
        if len(kept_sources) < MAX_SOURCES:
            kept_sources.add(event["source_ref"])
    visible_events = [event for event in events if event["source_ref"] in kept_sources][:MAX_EVENTS]
    try:
        playable = event_audio.available(
            settings.data_dir, [e["key"] for e in visible_events], time.time()
        )
    except (sqlite3.Error, OSError, ValueError):
        playable = set()
    for event in visible_events:
        if event["key"] in playable:
            event["audio_available"] = True
    chosen_groups = {key for key, _ in selected}
    metadata = {
        **view,
        "sources": [source for source in view["sources"] if source["source_ref"] in kept_sources],
        "segments": [
            {**segment, "coverage": _bounded_coverage(segment["coverage"])}
            for segment in view["segments"]
            if (segment["source_ref"], segment["boot_ref"]) in chosen_groups
        ],
        "coverage": _bounded_coverage(view["coverage"]),
        "record_count": len(rows),
        "displayed_minute_count": sum(len(records) for _, records in selected),
        "series_truncated": len(grouped) > MAX_GROUPS,
        "events_truncated": len(events) > len(visible_events),
        "channel_summaries": summaries,
        "summary_scope": "returned_original_windows_not_sleep_completeness",
        "max_groups": MAX_GROUPS,
        "max_events": MAX_EVENTS,
    }
    return {"series": series, "events": visible_events, "environment": metadata}
