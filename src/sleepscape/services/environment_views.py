"""Browser-safe environment time views; no analysis readiness or model calls."""

import hashlib
from datetime import UTC, datetime

from sleepscape.config import Settings
from sleepscape.domain.environment_time import (
    TIME_MAPPING_QUALITY_CODES,
    summarize_intervals,
    us_to_utc,
    utc_to_us,
)
from sleepscape.storage.environment_reader import EnvironmentReader


def _ref(kind: str, *identity: str) -> str:
    # Public references remain stable across reads/backups but contain no source identifiers.
    digest = hashlib.sha256("\0".join((kind, *identity)).encode()).hexdigest()[:24]
    return f"{kind}-{digest}"


def _window(start_us: int, end_us: int) -> dict:
    return {"utc_start": us_to_utc(start_us), "utc_end": us_to_utc(end_us)}


def _coverage(intervals: list[tuple[int, int]], start_us: int, end_us: int) -> dict:
    summary = summarize_intervals(intervals, start_us, end_us)
    return {
        "gaps": [_window(*window) for window in summary["gaps"]],
        "overlaps": [_window(*window) for window in summary["overlaps"]],
        "covered_duration_ms": summary["covered_us"] / 1000,
        "overlap_duration_ms": summary["overlap_us"] / 1000,
    }


def _source(row: dict) -> dict:
    profile = row["profile"]
    result = {
        "source_ref": _ref("source", row["device_id"], row["profile_id"]),
        "device_ref": _ref("device", row["device_id"]),
        "placement_ref": _ref("placement", row["device_id"], profile["placement_id"]),
        "sample_rate_hz": profile["sample_rate_hz"],
        "temperature_period_ms": profile["temperature_period_ms"],
        "light_period_ms": profile["light_period_ms"],
        "aggregation_ms": profile["aggregation_ms"],
        "sound_unit": "dBFS",
        "sound_absolute_calibration": "not_established",
        "light_measurement": None,
        "hardware_model": profile["hardware_model"]
        if profile["hardware_model"] in {"m5stack-c008-c"}
        else "unknown",
        "firmware_version": profile["firmware_version"]
        if profile["firmware_version"] in {"0.2.0", "0.3.0"}
        else "unknown",
        "temperature_sensor": profile["temperature_sensor"]
        if profile["temperature_sensor"] in {"dfrobot-sen0528-aht20"}
        else "unknown",
        "light_sensor": profile["light_sensor"]
        if profile["light_sensor"] in {"dfrobot-sen0228-veml7700"}
        else "unknown",
        "microphone": profile["microphone"] if profile["microphone"] == "spm1423" else "unknown",
        "sound_processing": "fixed-no-agc"
        if profile["sound_processing"] == "pdm-pcm16-fixed-no-agc"
        else "unknown",
        "event_detection": {
            name: profile[name]
            for name in (
                "event_baseline_seconds",
                "event_start_delta_db",
                "event_stop_delta_db",
                "event_stop_seconds",
                "event_merge_gap_seconds",
            )
        },
    }
    if light := profile.get("light_measurement"):
        result["light_measurement"] = {
            name: light[name]
            for name in (
                "gain",
                "integration_ms",
                "resolution_lux",
                "auto_range",
                "saturation_raw",
                "range_min_lux",
                "range_max_lux",
                "low_light_limit_lux",
            )
        }
    return result


def _record(row: dict, algorithm: str) -> dict:
    value = row["record"]
    result = {
        "record_ref": _ref(
            "record", row["device_id"], row["boot_id"], row["record_type"], str(row["seq"])
        ),
        "record_type": row["record_type"],
        "source_ref": _ref("source", row["device_id"], row["profile_id"]),
        "boot_ref": _ref("boot", row["device_id"], row["boot_id"]),
        **_window(row["utc_start_us"], row["utc_end_us"]),
        "duration_ms": row["mono_end_ms"] - row["mono_start_ms"],
        "time_quality": row["time_quality"],
        "time_uncertainty_ms": row["time_uncertainty_ms"],
        "anchor_ids": [
            _ref("anchor", row["device_id"], row["boot_id"], anchor) for anchor in row["anchor_ids"]
        ],
        "time_mapping_algorithm": algorithm,
        "time_mapping_version": row["mapping_version"],
        "time_index_revision": row["time_index_revision"],
        "time_flags": [flag for flag in row["time_flags"] if flag in TIME_MAPPING_QUALITY_CODES],
        "quality": value["quality"],
        "received_at": row["received_at"],
    }
    if row["record_type"] == "minute":
        # The validated channel contract contains only numerical/quality fields, no identities.
        result["metrics"] = value["metrics"]
    else:
        result["event"] = {
            name: value[name]
            for name in (
                "event_kind",
                "active_duration_ms",
                "max_1s_rms_dbfs",
                "baseline_dbfs",
                "merged",
                "incomplete",
                "continued_from_previous",
                "continues_next",
            )
        }
        result["event"]["envelope_is_continuous_sound"] = False
    return result


def _segments(rows: list[dict], start_us: int, end_us: int, truncated: bool) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        if row["record_type"] == "minute":
            identity = row["device_id"], row["boot_id"], row["profile_id"]
            groups.setdefault(identity, []).append(row)
    segments = []
    for (device, boot, profile), values in groups.items():
        left = max(start_us, min(row["utc_start_us"] for row in values))
        right = min(end_us, max(row["utc_end_us"] for row in values))
        intervals = [(row["utc_start_us"], row["utc_end_us"]) for row in values]
        segments.append(
            {
                "source_ref": _ref("source", device, profile),
                "boot_ref": _ref("boot", device, boot),
                **_window(left, right),
                "record_count": len(values),
                "coverage_scope": "this_boot_and_configuration_visible_extent",
                "coverage": None if truncated else _coverage(intervals, left, right),
            }
        )
    return segments


class EnvironmentViewService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def timeline(
        self,
        start: datetime,
        end: datetime,
        *,
        limit: int = 10000,
        reader: EnvironmentReader | None = None,
    ) -> dict:
        if reader is None:
            with EnvironmentReader(self.settings.data_dir) as local:
                stored = local.timeline(start, end, limit=limit)
        else:
            stored = reader.timeline(start, end, limit=limit)
        start_us, end_us = utc_to_us(start), utc_to_us(end)
        rows = stored["records"]
        sources = {}
        for row in rows:
            source = _source(row)
            sources[source["source_ref"]] = source
        minute_windows = [
            (row["utc_start_us"], row["utc_end_us"])
            for row in rows
            if row["record_type"] == "minute"
        ]
        coverage = None
        if stored["state"] == "available" and not stored["truncated"]:
            coverage = _coverage(minute_windows, start_us, end_us)
        segments = _segments(rows, start_us, end_us, stored["truncated"])
        multiple_sources = len(sources) > 1
        multiple_devices = len({source["device_ref"] for source in sources.values()}) > 1
        return {
            "state": stored["state"],
            **_window(start_us, end_us),
            "records": [_record(row, stored["algorithm"]) for row in rows],
            "sources": list(sources.values()),
            "segments": segments,
            "truncated": stored["truncated"],
            "limit": stored["limit"],
            "coverage": coverage,
            "coverage_scope": "mapped_minute_windows_only",
            "coverage_is_channel_completeness": False,
            "selected_source": None,
            "analysis_readiness_evaluated": False,
            "configuration_boundaries": multiple_sources,
            "boot_boundaries": len({segment["boot_ref"] for segment in segments}) > 1,
            "ambiguous_sources": multiple_devices
            or bool(multiple_sources and coverage and coverage["overlaps"]),
            "overlaps_require_source_review": bool(
                len(segments) > 1 and coverage and coverage["overlaps"]
            ),
            "source_boundaries_preserved": True,
            "global_time_state": stored["global_time_state"],
            "time_mapping_algorithm": stored["algorithm"],
            "time_index_version": stored["index_version"],
            "demo": self.settings.demo,
        }

    def status(self, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        now_us = utc_to_us(now)
        with EnvironmentReader(self.settings.data_dir) as reader:
            stored = reader.status()
        devices = []
        for row in stored["devices"]:
            received_at = row["last_received_at"]
            age = (now_us - utc_to_us(received_at)) / 1_000_000 if received_at else None
            receive_state = (
                "not_indexed"
                if row["receive_state"] == "not_indexed"
                else "never_received"
                if age is None
                else "clock_conflict"
                if age < 0
                else "recently_received"
                if age <= 900
                else "not_recently_received"
            )
            snapshot = None
            if latest := row["last_received_snapshot"]:
                value = latest["status"]
                snapshot = {
                    "received_at": latest["received_at"],
                    "boot_ref": _ref("boot", row["device_id"], latest["boot_id"]),
                    "queued_records": value["queued_records"],
                    "quarantined_count": value["quarantined_count"],
                    "lost_counts": value["lost_counts"],
                    "loss_range_count": len(value["lost_ranges"]),
                    "ranges_truncated": value["ranges_truncated"],
                    "quality": value["quality"],
                    "scope": "last_received_snapshot_not_current_device_state",
                }
            capture = None
            profile = None
            channel_health = None
            if mapped := row["last_mapped_capture"]:
                capture = {
                    "utc_start": us_to_utc(mapped["utc_start_us"]),
                    "utc_end": us_to_utc(mapped["utc_end_us"]),
                    "time_quality": mapped["time_quality"],
                    "boot_ref": _ref("boot", row["device_id"], mapped["boot_id"]),
                    "received_at": us_to_utc(utc_to_us(mapped["received_at"])),
                }
                profile = _source({**mapped, "device_id": row["device_id"]})
                channel_health = {
                    metric: {
                        name: channel[name]
                        for name in (
                            "state",
                            "expected_count",
                            "observed_count",
                            "valid_count",
                            "quality",
                        )
                    }
                    for metric, channel in mapped["record"]["metrics"].items()
                }
            devices.append(
                {
                    "device_ref": _ref("device", row["device_id"]),
                    "enabled": row["enabled"],
                    "receive_state": receive_state,
                    "receive_recent_threshold_seconds": 900,
                    "last_received_at": received_at,
                    "received_age_seconds": age,
                    "last_mapped_capture": capture,
                    "capture_state": row["capture_state"],
                    "last_received_snapshot": snapshot,
                    "last_measurement_profile": profile,
                    "channel_health": channel_health,
                    "channel_health_scope": "last_mapped_minute_not_live",
                    "currently_collecting": None,
                }
            )
        return {
            "state": stored["state"],
            "devices": devices,
            "devices_truncated": stored["devices_truncated"],
            "global_time_state": stored["global_time_state"],
            "time_mapping_algorithm": stored["algorithm"],
            "time_index_version": stored["index_version"],
            "checked_at": us_to_utc(now_us),
            "demo": self.settings.demo,
        }
