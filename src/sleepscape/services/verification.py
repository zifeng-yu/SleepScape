"""Offline reconciliation of stored raw responses and current normalized records."""

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from statistics import median

from sleepscape.domain.calendar import dates
from sleepscape.domain.reconcile import reconcile
from sleepscape.domain.training_load import load_comparison
from sleepscape.storage.repository import Repository


def _latest_attempts(attempts, first: str, last: str):
    result = {}
    for attempt in attempts:
        begin = max(attempt.get("start_date") or first, first)
        finish = min(attempt.get("end_date") or last, last)
        if begin > finish:
            continue
        for day in dates(date.fromisoformat(begin), date.fromisoformat(finish)):
            result[(attempt["resource"], day.isoformat())] = attempt
    return result


def recorded_loads(records, latest, first: str, last: str):
    activities = defaultdict(list)
    daily = {
        r["date"]
        for r in records
        if r["resource"] == "daily_summaries" and r["kind"] == "daily_summary"
    }
    for record in records:
        if record["kind"] == "activity" and record["resource"] == "activities":
            activities[record["date"]].append(record)
    results = []
    for day in dates(date.fromisoformat(first), date.fromisoformat(last)):
        value = day.isoformat()
        attempt = latest.get(("activities", value))
        daily_attempt = latest.get(("daily_summaries", value))
        complete = bool(
            attempt
            and attempt["status"] in {"ok", "partial", "empty"}
            and attempt["metrics"].get("pagination_complete") is True
        )
        rows = activities[value]
        loads = [row["data"].get("training_load") for row in rows]
        valid_loads = [v for v in loads if isinstance(v, (int, float)) and v >= 0]
        status, total = "unknown", None
        if complete:
            if rows and len(loads) == len(valid_loads):
                status, total = "recorded_activities", sum(valid_loads)
            elif rows:
                status = "partial_activity_load"
            elif value in daily and daily_attempt and daily_attempt["status"] in {"ok", "partial"}:
                status, total = "no_recorded_activity", 0
        results.append(
            {
                "date": value,
                "status": status,
                "activity_count": len(rows),
                "known_load_sum": sum(valid_loads),
                "load": total,
            }
        )
    return results


def display_readiness(
    records,
    daily_loads,
    detail: str,
    last: str,
    latest: dict,
    complete_day_through: str | None = None,
) -> dict:
    """Verify data prerequisites for the planned views, without claiming UI acceptance."""
    sleeps = {r["date"]: r for r in records if r["resource"] == "sleep" and r["kind"] == "sleep"}
    hrv = {r["date"]: r for r in records if r["resource"] == "hrv" and r["kind"] == "hrv_daily"}
    counts, complete_counts = Counter(), Counter()
    windows = []
    for day in dates(date.fromisoformat(detail), date.fromisoformat(last)):
        value = day.isoformat()
        provisional = complete_day_through is not None and value > complete_day_through
        day_counts = Counter()
        sleep_row, hrv_row = sleeps.get(value), hrv.get(value)
        sleep = sleep_row["data"] if sleep_row else {}
        night_hrv = hrv_row["data"] if hrv_row else {}
        comparison = load_comparison(daily_loads, (day - timedelta(days=1)).isoformat())
        sleep_fresh = latest.get(("sleep", value), {}).get("status") in {"ok", "partial"}
        hrv_fresh = latest.get(("hrv", value), {}).get("status") in {"ok", "partial"}
        day_counts["sleep_average_heart_rate_days"] += bool(
            sleep_fresh and sleep.get("average_heart_rate") is not None
        )
        day_counts["night_average_hrv_days"] += bool(
            hrv_fresh and night_hrv.get("night_average_ms") is not None
        )
        day_counts["previous_day_load_days"] += comparison["target_load"] is not None
        day_counts["full_30_day_baseline_days"] += comparison["baseline_valid_days"] == 30
        day_counts["percentage_displayable_days"] += comparison["change_percent"] is not None
        day_counts["stage_timeline_available_days"] += bool(
            sleep_fresh and sleep.get("stage_timeline_available")
        )
        start, finish = sleep.get("start_at"), sleep.get("end_at")
        start = datetime.fromisoformat(start) if start else None
        finish = datetime.fromisoformat(finish) if finish else None
        valid_window = bool(sleep_fresh and start and finish and start < finish)
        day_counts["sleep_window_days"] += valid_window
        samples = {}
        for kind, field in (("heart_rate_sample", "bpm"), ("hrv_sample", "hrv_ms")):
            points = [r for r in records if r["kind"] == kind and r["timestamp"]]
            valid = [
                r
                for r in points
                if valid_window
                and start <= datetime.fromisoformat(r["timestamp"]) <= finish
                and r["data"].get(field) is not None
                and latest.get((r["resource"], r["date"]), {}).get("status") in {"ok", "partial"}
            ]
            samples[kind] = len(valid)
            day_counts[f"{kind}_in_sleep_window_days"] += bool(valid)
        counts.update(day_counts)
        if not provisional:
            complete_counts.update(day_counts)
        windows.append(
            {
                "sleep_date": value,
                "is_provisional": provisional,
                "state": "pending" if provisional else "complete_day",
                "valid_window": valid_window,
                "sample_counts": samples,
                "time_coverage_ratio": None,
            }
        )
    return {
        "expected_sleep_days": len(windows),
        "complete_day_through": complete_day_through,
        "complete_sleep_days": sum(not item["is_provisional"] for item in windows),
        "provisional_sleep_days": sum(item["is_provisional"] for item in windows),
        "available_days": dict(counts),
        "complete_day_available_days": dict(complete_counts),
        "sleep_windows": windows,
        "validation_level": "stored_data_contract_only; not browser or sensor acceptance",
        "sleep_stages_fallback": (
            "sleep window plus cumulative stage totals; never synthesize intervals"
        ),
    }


def verify(repository: Repository) -> dict:
    run = repository.latest_run()
    if run is None:
        return {"status": "no_collection_run", "integrity": repository.integrity_check()}
    plan = run["plan"]
    first, last, detail = plan["history_start"], plan["end_date"], plan["detail_start"]
    complete_day = min(plan.get("complete_day_through") or last, last)
    load_first = min(first, (date.fromisoformat(detail) - timedelta(days=31)).isoformat())
    records = repository.list_records(start_date=first, end_date=last)
    attempts = repository.list_requests()
    latest = _latest_attempts(attempts, first, last)
    # A rolling collection window is not the historical scope required by the view.
    # Read only the extra activity/daily evidence, keeping coverage on the run's scope.
    load_records = [r for r in records if r["resource"] in {"activities", "daily_summaries"}]
    if load_first < first:
        before_first = (date.fromisoformat(first) - timedelta(days=1)).isoformat()
        for resource in ("activities", "daily_summaries"):
            load_records.extend(
                repository.list_records(
                    resource=resource,
                    start_date=load_first,
                    end_date=before_first,
                )
            )
    load_latest = _latest_attempts(attempts, load_first, last)
    load_stale = [
        r
        for r in load_records
        if load_latest.get((r["resource"], r["date"]), {}).get("status") in {"failed", "invalid"}
    ]
    current_attempts = repository.list_requests(run["id"])
    independent = {}
    supported = {
        "sleep",
        "sleep_summaries",
        "hrv",
        "hrv_summaries",
        "heart_rate",
        "daily_summaries",
        "activities",
    }
    for attempt in current_attempts:
        resource = attempt["resource"]
        if resource not in supported or attempt["status"] not in {"ok", "partial", "empty"}:
            continue
        scoped = [
            r
            for r in repository.list_records(
                resource=resource, start_date=attempt["start_date"], end_date=attempt["end_date"]
            )
            if r["request_key"] == attempt["request_key"]
        ]
        values = reconcile(resource, repository.get_raw(resource, attempt["request_key"]), scoped)
        counter = independent.setdefault(resource, Counter())
        counter.update(values)
    stale = []
    for record in records:
        attempt = latest.get((record["resource"], record["date"]))
        if attempt and attempt["status"] in {"failed", "invalid"}:
            stale.append(record)
    issues = Counter(
        (issue.get("severity", "warning"), issue.get("code", "unknown"))
        for attempt in current_attempts
        for issue in attempt["issues"]
    )
    coverage = {}
    fields = {
        "sleep": (
            "sleep",
            ["start_at", "end_at", "sleep_seconds", "average_heart_rate", "average_respiration"],
        ),
        "hrv": ("hrv_daily", ["night_average_ms", "weekly_average_ms"]),
        "stress": ("stress_daily", ["average_stress"]),
        "steps": ("steps_daily", ["steps"]),
        "daily_summaries": ("daily_summary", ["steps", "average_stress"]),
        "activities": ("activity", ["start_at", "end_at", "training_load"]),
    }
    for resource, (kind, names) in fields.items():
        rows = [
            r
            for r in records
            if r["resource"] == resource
            and r["kind"] == kind
            and (resource not in {"sleep", "hrv"} or r["date"] >= detail)
        ]
        coverage[resource] = {
            "record_count": len(rows),
            "calendar_days": len({r["date"] for r in rows}),
            "field_present_counts": {
                name: sum(r["data"].get(name) is not None for r in rows) for name in names
            },
        }
    series = {}
    for kind, value_name in (
        ("heart_rate_sample", "bpm"),
        ("hrv_sample", "hrv_ms"),
        ("body_battery_sample", "level"),
        ("spo2_sample", "percent"),
    ):
        series_start = (
            (date.fromisoformat(detail) - timedelta(days=1)).isoformat()
            if kind == "heart_rate_sample"
            else detail
        )
        rows = [r for r in records if r["kind"] == kind and r["date"] >= series_start]
        groups = defaultdict(list)
        for row in rows:
            groups[row["data"].get("series_type", "native")].append(row)
        series[kind] = {}
        for group, values in groups.items():
            times = sorted({r["timestamp"] for r in values if r["timestamp"]})
            intervals = [
                (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
                for a, b in zip(times, times[1:], strict=False)
            ]
            series[kind][group] = {
                "samples": len(values),
                "valid_samples": sum(
                    r["data"].get(value_name) is not None
                    and r["timestamp"] is not None
                    and r["data"].get("source_plottable") is not False
                    for r in values
                ),
                "observed_median_interval_seconds": median(intervals) if intervals else None,
                "time_coverage_ratio": None,
                "coverage_note": (
                    "native expected cadence not independently established; no imputation"
                ),
            }
    reconciliation = Counter()
    for row in records:
        if row["resource"] != "sleep" or row["kind"] != "sleep" or row["date"] < detail:
            continue
        original = repository.get_raw("sleep", row["request_key"])
        if isinstance(original, dict):
            matches = original.get("sleepTimeSeconds") == row["data"].get("sleep_seconds")
            reconciliation[
                "sleep_duration_matches_raw" if matches else "sleep_duration_mismatch"
            ] += 1
            for name, raw_key in (
                ("start_at", "sleepStartTimestampGMT"),
                ("end_at", "sleepEndTimestampGMT"),
            ):
                raw_time, normalized = original.get(raw_key), row["data"].get(name)
                if isinstance(raw_time, (int, float)) and normalized:
                    difference = abs(
                        datetime.fromisoformat(normalized).timestamp() - raw_time / 1000
                    )
                    reconciliation[
                        "sleep_utc_ms_matches" if difference < 0.001 else "sleep_utc_ms_mismatch"
                    ] += 1
        if row["data"].get("stage_timeline_available") is False:
            reconciliation["sleep_without_stage_timeline"] += 1
    daily_loads = recorded_loads(load_records, load_latest, load_first, last)
    for item in daily_loads:
        item["is_provisional"] = item["date"] > complete_day
        if item["is_provisional"]:
            item.update(status="pending", load=None)
    integrity = repository.integrity_check()
    failed = sum(a["status"] in {"failed", "invalid"} for a in current_attempts)
    attention = (
        failed
        or stale
        or load_stale
        or integrity != ["ok"]
        or run["status"] != "completed"
        or any(severity == "error" for severity, _ in issues)
        or any("mismatch" in name and count for name, count in reconciliation.items())
        or any(
            c.get("mismatches", 0) or c.get("source_shape_errors", 0) for c in independent.values()
        )
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run["id"],
        "collection_status": run["status"],
        "plan": plan,
        "status": "needs_attention" if attention else "usable_with_limitations",
        "integrity": integrity,
        "database_counts": repository.counts(),
        "request_status_counts": dict(Counter(a["status"] for a in current_attempts)),
        "resource_coverage": coverage,
        "normalized_counts": dict(Counter(r["kind"] for r in records)),
        "series": series,
        "issues": [
            {"severity": severity, "code": code, "count": count}
            for (severity, code), count in sorted(issues.items())
        ],
        "stale_retained_records": len(stale),
        "load_stale_retained_records": len(load_stale),
        "reconciliation": dict(reconciliation),
        "independent_reconciliation": {k: dict(v) for k, v in independent.items()},
        "daily_recorded_loads": daily_loads,
        "load_evaluation_window": {
            "start_date": load_first,
            "end_date": last,
            "complete_day_through": complete_day,
            "source": "stored_records_and_request_status",
        },
        "load_comparison": load_comparison(daily_loads, complete_day),
        "sleep_day_load_comparisons": [
            {
                "sleep_date": day.isoformat(),
                "is_provisional": day.isoformat() > complete_day,
                "sleep_state": "pending" if day.isoformat() > complete_day else "complete_day",
                **load_comparison(daily_loads, (day - timedelta(days=1)).isoformat()),
            }
            for day in dates(date.fromisoformat(detail), date.fromisoformat(last))
        ],
        "display_readiness": display_readiness(
            records,
            daily_loads,
            detail,
            last,
            latest,
            complete_day,
        ),
        "known_limitations": [
            "sleep stage timeline not documented; summaries must not be expanded into intervals",
            "no documented stress/respiration series or training readiness/recovery-time endpoint",
            "values are Garmin estimates; raw/API consistency is not independent sensor validation",
            "no-recorded-activity days do not prove no exercise or perfect device synchronization",
        ],
        "requests": current_attempts,
    }


def public_summary(report: dict) -> dict:
    """Print metadata and counts only, never personal measurement values."""
    return {
        key: report[key]
        for key in (
            "status",
            "collection_status",
            "integrity",
            "database_counts",
            "request_status_counts",
            "normalized_counts",
            "stale_retained_records",
            "load_stale_retained_records",
            "reconciliation",
            "independent_reconciliation",
            "issues",
        )
        if key in report
    }
