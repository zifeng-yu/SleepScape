"""Collect first, then check facts and run independently budgeted report generation."""

import sqlite3
import time
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

from sleepscape.adapters.catalog import PROVISIONAL_RESOURCES, build_daily_plan
from sleepscape.domain.daily import instant, observations, safe_code, schedule, window
from sleepscape.services.collection import CollectionError, collect, collection_lock
from sleepscape.storage.daily_runtime import (
    MAX_DAYS,
    DailyRuntimeError,
    collection_busy,
    collection_result,
    cooldown,
    read_state,
    write_state,
)
from sleepscape.storage.reader import DashboardReader, StorageReadError


def prepare_daily(settings, *, now=None):
    """A pure plan: does not inspect files, credentials, state or the network."""
    now = instant(now if now is not None else datetime.now(UTC))
    count = settings.garmin_lookback_days
    period = window(now, count)
    today = date.fromisoformat(period["end_date"])
    history = count + 30
    plan = build_daily_plan(today, count)
    return plan, {
        "mode": "sync",
        "scheduler": "daily",
        "sync_strategy": "bounded_daily",
        "end_date": period["end_date"],
        "detail_start": period["start_date"],
        "detail_days": count,
        "history_days": history,
        "history_start": (today - timedelta(days=history)).isoformat(),
        "complete_day_through": (today - timedelta(days=1)).isoformat(),
        "provisional_dates": [today.isoformat()],
        "provisional_resources": sorted(PROVISIONAL_RESOURCES),
        "timezone": "Asia/Shanghai",
        "planned_requests": len(plan),
        "analysis": "separate_report_stage",
    }


def _observations(settings, period):
    first, last = (date.fromisoformat(period[key]) for key in ("start_date", "end_date"))
    try:
        with DashboardReader(settings.data_dir) as reader:
            records, attempts = reader.summaries(first, last), reader.attempts(first, last)
        return observations(first, last, records, attempts), None
    except (StorageReadError, sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return [], "sleep_observations_unavailable"


def _counts(summary, planned):
    result = {"planned": planned, "completed": 0, "failed": 0, "http": None}
    for target, source in (
        ("completed", "completed_resources"),
        ("failed", "failed_resources"),
        ("http", "http_requests"),
    ):
        value = summary.get(source)
        if type(value) is int and 0 <= value <= 1_000_000:
            result[target] = value
    return result


def _finish(row, summary, now):
    status = summary.get("status")
    row["status"] = (
        status if status in {"completed", "partial", "failed", "interrupted"} else "failed"
    )
    row["phase"] = "finished"
    row["finished_at"] = now.isoformat()
    row["request_counts"] = _counts(summary, row["request_counts"]["planned"])
    row["stop_reason"] = safe_code(summary["stop_reason"]) if summary.get("stop_reason") else None
    row["error_code"] = row["stop_reason"]
    if summary.get("retry_not_before"):
        try:
            row["retry_not_before"] = instant(
                datetime.fromisoformat(summary["retry_not_before"])
            ).isoformat()
        except (TypeError, ValueError):
            row["error_code"] = "daily_state_unavailable"


def _recover(settings, state, now):
    """Caller has the collection mutex, or has confirmed it is currently unlocked."""
    for row in state["days"]:
        if instant(datetime.fromisoformat(row["started_at"])) > now:
            raise DailyRuntimeError("daily_clock_regressed")
        if row["status"] == "running":
            summary = collection_result(settings.data_dir, row["run_id"])
            _finish(
                row,
                summary or {"status": "interrupted", "stop_reason": "previous_run_interrupted"},
                now,
            )


def _public(row):
    return {key: value for key, value in row.items() if key != "run_id"}


def daily_status(settings, *, now=None):
    """A read-only projection; next_planned_at is not proof of an installed scheduler."""
    now = instant(now if now is not None else datetime.now(UTC))
    period = window(now, settings.garmin_lookback_days)
    result = {
        "state": "not_run",
        "lookback_days": settings.garmin_lookback_days,
        "schedule": schedule(now),
        "latest": None,
        "history": [],
        "analysis": "automatic_enabled" if settings.llm_automatic_enabled else "automatic_disabled",
        "error_code": None,
    }
    result["observations"], result["observations_error_code"] = _observations(settings, period)
    try:
        state = read_state(settings.data_dir)
        if not collection_busy(settings.data_dir):
            _recover(settings, state, now)
        rows = [_public(row) for row in reversed(state["days"])]
        result.update(
            state="available" if rows else "not_run", latest=rows[0] if rows else None, history=rows
        )
    except DailyRuntimeError as exc:
        result.update(state="unavailable", error_code=exc.code)
    return result


def _result(settings, row, action, period, *, error_code=None):
    values, observation_error = _observations(settings, period)
    return {
        "status": row["status"] if row else "blocked",
        "action": action,
        "run": deepcopy(row),
        "observations": values,
        "observations_error_code": observation_error,
        "analysis": "separate_report_stage",
        "error_code": error_code or (row or {}).get("error_code"),
    }


def _run_daily(settings, *, now=None, retry=False, gateway=None):
    """One admission per Shanghai day; retry is explicit and never repeats a completed day."""
    clock = instant(now if now is not None else datetime.now(UTC))
    fixed_clock = now is not None

    def finished():
        return clock if fixed_clock else datetime.now(UTC)

    plan, metadata = prepare_daily(settings, now=clock)
    period = {"start_date": metadata["detail_start"], "end_date": metadata["end_date"]}
    try:
        with collection_lock(settings.data_dir):
            state = read_state(settings.data_dir)
            _recover(settings, state, clock)
            existing = next((r for r in state["days"] if r["day"] == period["end_date"]), None)
            if existing and (not retry or existing["status"] == "completed"):
                # Persist crash reconciliation, but never start a second network attempt implicitly.
                write_state(settings.data_dir, state)
                action = "retry_not_allowed" if retry else "skipped_existing"
                return _result(settings, existing, action, period)
            deadline = cooldown(settings.data_dir)
            if deadline and deadline > clock:
                if existing:
                    return _result(
                        settings,
                        existing,
                        "blocked",
                        period,
                        error_code="retry_after_wait_still_active",
                    )
                row = _new_row(period, plan, clock, 1)
                row.update(
                    status="deferred",
                    phase="deferred",
                    finished_at=clock.isoformat(),
                    error_code="retry_after_wait_still_active",
                    retry_not_before=deadline.isoformat(),
                )
                state["days"].append(row)
                state["days"] = state["days"][-MAX_DAYS:]
                write_state(settings.data_dir, state)
                return _result(settings, row, "blocked", period)
            row = _new_row(period, plan, clock, existing["attempt"] + 1 if existing else 1)
            state["days"] = [r for r in state["days"] if r["day"] != row["day"]] + [row]
            state["days"] = state["days"][-MAX_DAYS:]
            write_state(settings.data_dir, state)

            def started(run_id):
                row.update(run_id=run_id, phase="collecting")
                write_state(settings.data_dir, state)

            try:
                summary = collect(
                    settings,
                    date.fromisoformat(row["day"]),
                    metadata["detail_days"],
                    metadata["history_days"],
                    gateway=gateway,
                    plan=plan,
                    metadata=metadata,
                    _lock_held=True,
                    on_start=started,
                )
                _finish(row, summary, finished())
            except KeyboardInterrupt:
                _finish(
                    row,
                    {"status": "interrupted", "stop_reason": "previous_run_interrupted"},
                    finished(),
                )
                write_state(settings.data_dir, state)
                raise
            except (CollectionError, DailyRuntimeError) as exc:
                code = safe_code(getattr(exc, "code", str(exc)))
                summary = collection_result(settings.data_dir, row["run_id"])
                _finish(row, summary or {"status": "failed", "stop_reason": code}, finished())
                if code == "retry_after_wait_still_active":
                    row.update(status="deferred", phase="deferred")
                    value = cooldown(settings.data_dir)
                    row["retry_not_before"] = value.isoformat() if value else None
            except Exception:
                _finish(
                    row, {"status": "failed", "stop_reason": "local_collection_error"}, finished()
                )
            write_state(settings.data_dir, state)
            return _result(settings, row, "executed", period)
    except (CollectionError, DailyRuntimeError) as exc:
        return _result(
            settings, None, "blocked", period, error_code=safe_code(getattr(exc, "code", str(exc)))
        )


def _new_row(period, plan, now, attempt):
    return {
        "day": period["end_date"],
        "status": "running",
        "phase": "preparing",
        "attempt": attempt,
        "started_at": now.isoformat(),
        "finished_at": None,
        "run_id": None,
        "window": period,
        "request_counts": {"planned": len(plan), "completed": 0, "failed": 0, "http": None},
        "error_code": None,
        "stop_reason": None,
        "retry_not_before": None,
        "analysis": "separate_report_stage",
    }


def run_daily(settings, *, now=None, retry=False, gateway=None):
    """Collect first, then persist local readiness after releasing the collection mutex."""
    deadline = time.monotonic() + 7200
    result = _run_daily(settings, now=now, retry=retry, gateway=gateway)
    if result["action"] not in {"executed", "skipped_existing", "retry_not_allowed"}:
        result["readiness"] = {"status": "skipped", "code": "collection_not_finished"}
        return result
    from sleepscape.services.event_audio import reconcile

    try:
        reconcile(settings, now)
        result["event_audio"] = {"status": "completed"}
    except Exception:
        result["event_audio"] = {"status": "failed", "code": "audio_reconcile_unavailable"}

    from sleepscape.services.readiness import check_readiness
    from sleepscape.storage.analysis import AnalysisStorageError

    try:
        checked = check_readiness(settings, now=now)
        result["readiness"] = {
            "status": checked["status"],
            "window": checked["window"],
            "counts": checked["counts"],
            "persisted": checked["persisted"],
        }
    except AnalysisStorageError as exc:
        result["readiness"] = {
            "status": "not_initialized" if exc.code == "analysis_not_initialized" else "failed",
            "code": exc.code,
        }
    except Exception:
        result["readiness"] = {"status": "failed", "code": "analysis_readiness_unavailable"}
    # No collection mutex or SQLite transaction is held during remote model calls.
    from sleepscape.services.sleep_analysis import run_analysis

    try:
        result["reports"] = run_analysis(settings, automatic=True, now=now)
    except Exception:
        result["reports"] = {"status": "failed", "code": "sleep_analysis_state_unavailable"}
    result["analysis"] = result["reports"]["status"]
    if settings.llm_review_enabled and result["run"] is not None:
        from sleepscape.services.weekly_review import run_review

        result["review"] = run_review(
            settings, date.fromisoformat(result["run"]["day"]), now=now, deadline=deadline
        )
    return result
