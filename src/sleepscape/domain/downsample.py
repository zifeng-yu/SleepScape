"""Display-only min/max buckets. Original timestamps and empty gaps are preserved."""

import math
from datetime import UTC, datetime


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def minmax_downsample(
    points: list[dict],
    start: datetime,
    end: datetime,
    max_points: int = 2000,
) -> dict:
    """Return only observed finite values, using <= floor(max_points/2) time buckets."""
    if isinstance(max_points, bool) or not 2 <= max_points <= 2000:
        raise ValueError("invalid_max_points")
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("invalid_time_window")
    valid = []
    for point in points:
        timestamp, value = _timestamp(point.get("timestamp")), point.get("value")
        if (
            timestamp is not None
            and start <= timestamp <= end
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            valid.append((timestamp, point))
    valid.sort(key=lambda pair: pair[0])
    resolution = {
        "method": "none",
        "original_count": len(points),
        "valid_count": len(valid),
        "returned_count": len(valid),
        "bucket_seconds": None,
    }
    if len(valid) <= max_points:
        return {"points": [point.copy() for _, point in valid], "display_resolution": resolution}
    bucket_count = max_points // 2
    width = (end - start).total_seconds() / bucket_count
    buckets: dict[int, list] = {}
    for timestamp, point in valid:
        index = min(int((timestamp - start).total_seconds() / width), bucket_count - 1)
        buckets.setdefault(index, []).append((timestamp, point))
    retained = []
    for bucket in buckets.values():
        minimum = min(range(len(bucket)), key=lambda i: bucket[i][1]["value"])
        maximum = max(range(len(bucket)), key=lambda i: bucket[i][1]["value"])
        for index in sorted({minimum, maximum}):
            timestamp, point = bucket[index]
            retained.append((timestamp, {**point, "bucket_valid_count": len(bucket)}))
    retained.sort(key=lambda pair: pair[0])
    resolution.update(method="minmax", returned_count=len(retained), bucket_seconds=width)
    return {"points": [point for _, point in retained], "display_resolution": resolution}
