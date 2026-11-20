"""Post-collection seven-day generation and independent read-only history."""

import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sleepscape.domain.analysis_readiness import evaluate_readiness
from sleepscape.domain.daily import instant
from sleepscape.domain.dashboard_dates import require_visible_night
from sleepscape.domain.weekly_review import build_evidence, nightly_evidence, review_dates
from sleepscape.models.audit import safe_usage
from sleepscape.models.base import ModelError
from sleepscape.models.settings import configuration_fingerprint, load_model_settings
from sleepscape.resources.weekly_review import parse_output, request_for, resource_digest
from sleepscape.services.model_call import call_model
from sleepscape.services.readiness import _environment, _semantic
from sleepscape.services.sleep_analysis import CORRECTION_ERRORS, MAX_RETRY_WAIT, RETRY_ERRORS
from sleepscape.services.views import DashboardService
from sleepscape.storage import model_runtime as runtime
from sleepscape.storage import weekly_review as store
from sleepscape.storage.morning_feeling import read_range
from sleepscape.storage.reader import DashboardReader, StorageReadError
from sleepscape.storage.usage import CapacityError

FINALIZATION_MARGIN_SECONDS = 10
_TERMINAL = {"complete", "interrupted", "source_changed"}


def _enabled(settings):
    return settings.llm_review_enabled and settings.llm_automatic_enabled and not settings.demo


def _names(days):
    return [day.isoformat() for day in days]


def _snapshot(settings, reader, days, clock):
    view = DashboardService(settings)
    view._now = lambda: clock
    context = view.readiness_context(reader, days[0], days[-1])
    feeling_state, feelings = read_range(reader.connection, days[0], days[-1])
    entries, origins, groups = [], [], {}
    failures = []
    for index, day in enumerate(days, 1):
        night = view.readiness_night(reader, day, context=context, optional=False)
        environment = _environment(settings, reader, night, clock)
        result = evaluate_readiness(
            day, night, environment, now=clock, lookback_days=settings.garmin_lookback_days
        )
        if not result["core_ready"]:
            failures.append(
                {
                    "date": day.isoformat(),
                    "missing": [
                        dict(zip(("source", "metric", "code"), entry, strict=True))
                        for entry in sorted(
                            {
                                (item["source"], item["metric"], item["code"])
                                for item in result["missing"]
                            }
                        )
                    ],
                }
            )
            continue
        entry, identity = nightly_evidence(
            day, night, environment, result, now=clock, zone=view.zone
        )
        group = groups.setdefault(identity, f"G{len(groups) + 1}")
        entries.append(
            {
                "id": f"N{index}",
                **entry,
                "feeling": feelings.get(day.isoformat()),
                "environment_group": group,
            }
        )
        origins.append(
            {
                "identity": identity,
                "sleep": night["sleep"],
                "policy": result["policy_version"],
                "environment": result["environment_summary"],
                "warnings": result["warnings"],
            }
        )
    if failures:
        return None, None, failures
    evidence = build_evidence(entries, feeling_state)
    return evidence, store.digest(_semantic([evidence, origins])), []


def prepare_review(settings):
    """Explicit migration after a checked backup, without contacting the model."""
    from sleepscape.services.backup import check_restore, create_backup

    with DashboardReader(settings.data_dir) as reader:
        if not reader.present:
            raise store.ReviewStorageError("sleep_review_not_initialized")
        version = store.schema_version(reader.connection)
        if version == 2:
            return {"status": "already_initialized", "schema_version": 2, "backup_verified": False}
    backup = create_backup(settings)
    check_restore(settings, Path(backup["backup_path"]))
    store.migrate(settings.data_dir)
    return {
        "status": "upgraded" if version else "initialized",
        "schema_version": 2,
        "backup_verified": True,
    }


def _status(state, *, code=None, calls=0):
    return {"state": state, "error_code": code, "model_calls": calls}


def _generate(settings, days, clock, now_fn, ledger, *, caller, deadline):
    directory = settings.data_dir
    end = days[-1].isoformat()
    names = _names(days)
    with DashboardReader(directory) as reader:
        previous = store.load_run(reader.connection, end)
        if previous and previous["state"] in _TERMINAL:
            return _status(previous["state"], code=previous["error_code"])
        if (
            previous
            and previous["state"] == "failed"
            and (previous["error_code"] not in RETRY_ERRORS or previous["request_id"] is None)
        ):
            return _status("failed", code=previous["error_code"])
        if previous and previous["state"] == "running":
            # The shared model lock proves that there is no live review caller.
            store.finish(directory, end, "interrupted", "review_interrupted")
            return _status("interrupted", code="review_interrupted")
        try:
            evidence, source_hash, incomplete = _snapshot(settings, reader, days, clock)
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            sqlite3.Error,
            StorageReadError,
            ModelError,
        ):
            evidence = None
            incomplete = []
    if evidence is None:
        state = "incomplete_data" if incomplete else "readiness_unavailable"
        code = None if incomplete else "review_readiness_unavailable"
        store.record_check(
            directory, names, instant(clock).isoformat(), state, incomplete=incomplete, code=code
        )
        return {**_status(state, code=code), "incomplete": incomplete}
    try:
        model = load_model_settings(settings)
        if not model.enabled:
            raise ModelError("model_disabled")
        config_hash, contract_hash = configuration_fingerprint(settings), resource_digest()
        correction = bool(previous and previous["error_code"] in CORRECTION_ERRORS)
        request = request_for(evidence, correction=correction)
    except (ModelError, ValueError) as exc:
        code = exc.code if isinstance(exc, ModelError) else str(exc)
        code = code if code in store.STORAGE_CODES else "model_operation_failed"
        store.record_check(directory, names, clock.isoformat(), "blocked", code=code)
        return _status("blocked", code=code)

    if (
        deadline is not None
        and time.monotonic() + min(180, model.timeout_seconds) + FINALIZATION_MARGIN_SECONDS
        > deadline
    ):
        store.record_check(directory, names, clock.isoformat(), "blocked", code="review_deadline")
        return _status("blocked", code="review_deadline")

    started_at = now_fn()
    if runtime.budget(ledger.state, started_at)["remaining"] == 0:
        store.record_check(directory, names, clock.isoformat(), "blocked", code="budget_exhausted")
        return _status("blocked", code="budget_exhausted")
    input_id = store.claim(
        directory, evidence, source_hash, config_hash, contract_hash, started_at.isoformat()
    )
    if input_id is None:
        return _status("blocked", code="model_busy")
    try:
        request_id = ledger.reserve(config_hash, started_at)
    except ModelError as exc:
        store.finish(directory, end, "blocked", exc.code)
        return _status("blocked", code=exc.code)
    try:
        store.attach_request(directory, end, request_id)
    except Exception:
        ledger.finish(request_id, now_fn(), 0, error=ModelError("model_operation_failed"))
        raise
    began = time.monotonic()
    usage = {}
    try:
        reply = caller(model, request)
        usage = safe_usage(reply.usage)
        content = parse_output(reply.text, evidence)
    except Exception as exc:
        error = exc if isinstance(exc, ModelError) else ModelError("model_operation_failed")
        ledger.finish(
            request_id, now_fn(), int((time.monotonic() - began) * 1000), error=error, usage=usage
        )
        store.finish(directory, end, "failed", error.code)
        result = _status("failed", code=error.code, calls=1)
        delay = error.retry_after if error.retry_after is not None else 1
        if error.code in RETRY_ERRORS and (error.retryable or error.code in CORRECTION_ERRORS):
            if delay <= MAX_RETRY_WAIT:
                result["retry_delay"] = delay
        return result
    duration = int((time.monotonic() - began) * 1000)
    ledger.finish(request_id, now_fn(), duration, usage=usage)

    def guard(connection):
        try:
            with DashboardReader(directory, connection=connection) as reader:
                current, fingerprint, _ = _snapshot(settings, reader, days, now_fn())
            return (
                bool(current)
                and fingerprint == source_hash
                and (
                    resource_digest() == contract_hash
                    and configuration_fingerprint(settings) == config_hash
                )
            )
        except Exception:
            return False

    try:
        saved = store.publish(
            directory,
            end,
            input_id,
            request_id,
            now_fn().isoformat(),
            content,
            {"model": reply.model, "usage": usage, "duration_ms": duration},
            guard,
        )
    except Exception:
        store.finish(directory, end, "failed", "model_operation_failed")
        return _status("failed", code="model_operation_failed", calls=1)
    if not saved:
        store.finish(directory, end, "source_changed", "review_source_changed")
        return _status("source_changed", code="review_source_changed", calls=1)
    return _status("complete", calls=1)


def run_review(settings, day: date, *, now=None, caller=None, deadline=None, sleeper=time.sleep):
    """Use the daily model mutex and budget until publication or a stopping condition."""
    if not _enabled(settings):
        return _status("disabled")
    days = review_dates(day, settings.dashboard_start_date)
    if not days:
        return _status("empty_range")
    now_fn = (lambda: instant(now)) if now is not None else lambda: datetime.now(UTC)
    deadline = time.monotonic() + 7200 if deadline is None else deadline
    calls = 0
    try:
        with DashboardReader(settings.data_dir) as reader:
            if store.schema_version(reader.connection) < 2:
                return _status("not_initialized")
        with runtime.session(settings.data_dir) as ledger:
            while True:
                clock = now_fn()
                if clock.astimezone(runtime.ZONE).date() != day:
                    return _status("blocked", code="review_deadline", calls=calls)
                result = _generate(
                    settings,
                    days,
                    clock,
                    now_fn,
                    ledger,
                    caller=caller or call_model,
                    deadline=deadline,
                )
                calls += result["model_calls"]
                result["model_calls"] = calls
                delay = result.pop("retry_delay", None)
                if (
                    delay is None
                    or runtime.budget(ledger.state, now_fn())["remaining"] == 0
                    or time.monotonic() + delay >= deadline
                ):
                    return result
                sleeper(delay)
    except ModelError as exc:
        try:
            store.record_check(
                settings.data_dir, _names(days), now_fn().isoformat(), "blocked", code=exc.code
            )
        except (CapacityError, store.ReviewStorageError):
            pass
        return _status("blocked", code=exc.code, calls=calls)
    except CapacityError as exc:
        return _status("blocked", code=exc.code, calls=calls)
    except Exception:
        return _status("unavailable", code="review_readiness_unavailable", calls=calls)


def review_view(settings, day: date, *, now=None):
    """GET boundary. Preserve a published report even if source comparison fails."""
    require_visible_night(day, settings.dashboard_start_date)
    days = review_dates(day, settings.dashboard_start_date)
    start, end = days[0].isoformat(), day.isoformat()
    result = {
        "window": {"start_date": start, "end_date": end, "visible_n": len(days)},
        "state": "not_run" if _enabled(settings) else "disabled",
        "checked_at": None,
        "error_code": None,
        "incomplete": [],
        "report": None,
        "source_changed": None,
    }
    try:
        with DashboardReader(settings.data_dir) as reader:
            if not store.available(reader.connection):
                if _enabled(settings):
                    result["state"] = "not_initialized"
                return result
            run = store.load_run(reader.connection, end)
            if run is None or run["start_date"] != start:
                return result
            result.update(
                state=run["state"],
                incomplete=run["incomplete"],
                checked_at=run["checked_at"],
                error_code=run["error_code"] if run["error_code"] in store.STORAGE_CODES else None,
            )
            clock = instant(now or datetime.now(UTC))
            if run["state"] == "running" and clock - datetime.fromisoformat(
                run["checked_at"]
            ) > timedelta(hours=2):
                result["state"] = "interrupted"
            stored = store.load_report(reader.connection, end)
            if stored is not None:
                source_hash = stored.pop("source_hash")
                result["report"] = stored
                try:
                    _, fingerprint, incomplete = _snapshot(settings, reader, days, clock)
                    result["source_changed"] = bool(incomplete) or fingerprint != source_hash
                except Exception:
                    pass
    except Exception:
        if result["report"] is None:
            result.update(state="unavailable", checked_at=None, incomplete=[], error_code=None)
    return result
