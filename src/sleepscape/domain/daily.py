"""Pure contracts for daily collection; execution success is not sleep completeness."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")
STATUSES = frozenset({"running", "completed", "partial", "failed", "interrupted", "deferred"})
PHASES = frozenset({"preparing", "collecting", "finished", "deferred"})
ERROR_CODES = frozenset(
    {
        "storage_capacity_critical",
        "storage_capacity_unavailable",
        "daily_state_unavailable",
        "daily_clock_regressed",
        "collection_already_running",
        "retry_after_wait_still_active",
        "previous_run_interrupted",
        "collection_error",
        "local_collection_error",
        "authentication_rejected",
        "authentication_network_retry_exhausted",
        "expired_token",
        "authentication_network_error",
        "credential_file_unavailable",
        "authentication_invalid_response",
        "authorization_failed",
        "http_error",
        "invalid_json_response",
        "network_retry_exhausted",
        "retry_exhausted",
        "authentication_retry_exhausted",
        "invalid_api_response",
        "request_outside_allowed_scope",
        "redirect_blocked",
        "response_too_large",
        "credential_symlink_not_allowed",
        "credential_file_permissions",
        "credential_directory_permissions",
        "empty_key_file",
        "retry_after_deferred",
        "activity_page_invalid_shape",
        "activity_pagination_no_progress",
        "activity_page_invalid_id",
        "activity_pagination_overlap",
        "activity_pagination_limit",
        "sleep_observations_unavailable",
        "daily_configuration_invalid",
    }
)


def safe_code(value):
    return value if isinstance(value, str) and value in ERROR_CODES else "collection_error"


def instant(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("timezone_required")
    return now.astimezone(UTC)


def window(now: datetime, lookback_days: int) -> dict:
    if type(lookback_days) is not int or not 1 <= lookback_days <= 31:
        raise ValueError("invalid_lookback_days")
    day = instant(now).astimezone(ZONE).date()
    return {
        "start_date": (day - timedelta(days=lookback_days - 1)).isoformat(),
        "end_date": day.isoformat(),
    }


def schedule(now: datetime) -> dict:
    local = instant(now).astimezone(ZONE)
    next_run = datetime.combine(local.date(), time(9), ZONE)
    if next_run <= local:
        next_run += timedelta(days=1)
    return {
        "timezone": "Asia/Shanghai",
        "time": "09:00",
        "installation_state": "unknown",
        "next_planned_at": next_run.isoformat(),
        "next_run_kind": "planned_not_installed",
    }


def observations(first, last, records: list[dict], attempts: list[dict]) -> list[dict]:
    """Report usable sleep observations only, never dual-source readiness or health values."""
    result = []
    day = first
    while day <= last:
        key = day.isoformat()
        candidates = [r for r in records if r.get("kind") == "sleep" and r.get("date") == key]
        latest = {}
        for attempt in attempts:
            resource = attempt.get("resource")
            if (
                resource in {"sleep", "sleep_summaries"}
                and attempt["start_date"] <= key <= attempt["end_date"]
            ):
                latest[resource] = attempt
        usable = [
            r
            for r in candidates
            if isinstance(r.get("data", {}).get("sleep_seconds"), (int, float))
            and not isinstance(r["data"]["sleep_seconds"], bool)
            and r["data"]["sleep_seconds"] > 0
        ]
        fresh = [
            r
            for r in usable
            if latest.get(r["resource"], {}).get("status") not in {"failed", "invalid"}
        ]
        if fresh:
            state = "available"
        elif usable:
            state = "stale"
        elif any(a["status"] == "failed" for a in latest.values()):
            state = "request_failed"
        elif candidates or any(a["status"] == "invalid" for a in latest.values()):
            state = "invalid"
        else:
            state = "missing" if latest else "not_collected"
        result.append({"date": key, "sleep": state})
        day += timedelta(days=1)
    return result
