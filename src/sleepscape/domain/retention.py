"""Pure bounded backup retention; always preserve the newest verified copy."""

from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")


def _ordered(entries):
    seen = set()
    result = []
    for row in entries:
        if row["id"] in seen or type(row["size_bytes"]) is not int or row["size_bytes"] < 0:
            raise ValueError("invalid_retention_entry")
        seen.add(row["id"])
        timestamp = datetime.fromisoformat(row["created_at"])
        if timestamp.tzinfo is None:
            raise ValueError("invalid_retention_date")
        result.append((timestamp, row))
    return [
        row for _, row in sorted(result, key=lambda pair: (pair[0], pair[1]["id"]), reverse=True)
    ]


def _budget(ordered, candidates, max_bytes):
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("invalid_retention_budget")
    if not ordered:
        return {"keep": [], "delete": [], "over_budget": False}
    keep = [ordered[0]["id"]]
    size = ordered[0]["size_bytes"]
    for row in ordered[1:]:
        if row["id"] in candidates and size + row["size_bytes"] <= max_bytes:
            keep.append(row["id"])
            size += row["size_bytes"]
    return {
        "keep": keep,
        "delete": [row["id"] for row in ordered if row["id"] not in keep],
        "over_budget": size > max_bytes,
    }


def select_local(entries, keep_count=3, max_bytes=4 * 1024**3):
    if type(keep_count) is not int or not 1 <= keep_count <= 100:
        raise ValueError("invalid_retention_count")
    ordered = _ordered(entries)
    return _budget(ordered, {row["id"] for row in ordered[:keep_count]}, max_bytes)


def select_offsite(entries, daily_count=7, weekly_count=4, max_bytes=16 * 1024**3):
    if not 1 <= daily_count <= 31 or not 0 <= weekly_count <= 52:
        raise ValueError("invalid_retention_count")
    ordered = _ordered(entries)
    daily, weekly, keep = set(), set(), set()
    for row in ordered:
        local = datetime.fromisoformat(row["created_at"]).astimezone(ZONE)
        day, week = local.date(), local.date().isocalendar()[:2]
        if day not in daily and len(daily) < daily_count:
            daily.add(day)
            keep.add(row["id"])
        if week not in weekly and len(weekly) < weekly_count:
            weekly.add(week)
            keep.add(row["id"])
    return _budget(ordered, keep, max_bytes)
