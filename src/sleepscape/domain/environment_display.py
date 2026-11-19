"""Window-preserving display reduction, independent from storage and readiness."""

import math
from datetime import datetime


def finite(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def reduce_windows(points: list[dict], start: datetime, end: datetime, limit: int) -> dict:
    """Keep original windows including an intersecting left edge, never invent instants.

    Select the observed window with the lowest minimum and highest maximum in
    each bucket. Values/coverage remain attached to their original window; no
    synthetic mean or interpolation is produced. Segment ids survive reduction.
    """
    if type(limit) is not int or not 2 <= limit <= 2000:
        raise ValueError("invalid_max_points")
    result = {
        "method": "none",
        "original_count": len(points),
        "valid_count": sum(finite(point.get("value")) for point in points),
        "returned_count": len(points),
        "bucket_seconds": None,
    }
    if len(points) <= limit:
        return {"points": points, "display_resolution": result}
    count = limit // 2
    width = (end - start).total_seconds() / count
    buckets = {}
    for point in points:
        stamp = datetime.fromisoformat(point["window_start"])
        index = max(0, min(int((stamp - start).total_seconds() / width), count - 1))
        buckets.setdefault(index, []).append(point)
    selected = []
    for bucket in buckets.values():
        valid = [point for point in bucket if finite(point.get("value"))]
        if not valid:
            selected.append(bucket[0])
            continue
        low = min(valid, key=lambda p: p["min"] if finite(p.get("min")) else p["value"])
        high = max(valid, key=lambda p: p["max"] if finite(p.get("max")) else p["value"])
        selected.append(low)
        if high is not low:
            selected.append(high)
    selected.sort(key=lambda p: (p["window_start"], p["window_end"], p["record_ref"]))
    result.update(method="window_minmax", returned_count=len(selected), bucket_seconds=width)
    return {"points": selected, "display_resolution": result}
