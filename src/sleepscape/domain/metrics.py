"""The single public registry of supported timeline metrics; no I/O or mutable globals."""

from copy import deepcopy

_METRICS = (
    ("heart_rate", "心率", "bpm", "garmin", "#dc635d", 0, True, {}, ("native",)),
    ("hrv", "HRV", "ms", "garmin", "#8067bd", 0, True, {}, ("native",)),
    ("spo2", "血氧", "%", "garmin", "#4694bd", 0, True, {}, ("native", "hourly_average")),
    ("body_battery", "身体电量", "points", "garmin", "#bd9445", 0, True, {}, ("native",)),
    ("temperature_c", "温度", "°C", "environment", "#d77f47", 1, True, {}, ("minute_mean",)),
    (
        "relative_humidity_pct",
        "相对湿度",
        "%",
        "environment",
        "#479cac",
        1,
        False,
        {"min": 0, "max": 100},
        ("minute_mean",),
    ),
    (
        "illuminance_lux",
        "光照",
        "lx",
        "environment",
        "#9b913d",
        2,
        False,
        {"min": 0},
        ("minute_mean",),
    ),
    (
        "sound_relative",
        "相对声音",
        "dBFS",
        "environment",
        "#7462ac",
        1,
        True,
        {},
        ("minute_mean", "minute_peak"),
    ),
)


def metric_registry() -> dict:
    return {
        "version": "1",
        "items": [
            {
                "id": metric,
                "label": label,
                "unit": unit,
                "source": source,
                "color": color,
                "precision": precision,
                "default_visible": visible,
                "axis": deepcopy(axis),
                "series_types": list(types),
                "aggregation": "minute" if source == "environment" else "sample",
                "missing_policy": "gap",
            }
            for metric, label, unit, source, color, precision, visible, axis, types in _METRICS
        ],
    }


def metric_definition(metric: str) -> dict:
    for item in metric_registry()["items"]:
        if item["id"] == metric:
            return item
    raise ValueError("unknown_metric")
