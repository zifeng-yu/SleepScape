"""Read-only dual-source evaluation and explicit local check persistence; no model calls."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sleepscape.config import Settings
from sleepscape.domain.analysis_readiness import evaluate_readiness
from sleepscape.domain.daily import instant, schedule, window
from sleepscape.services.backup import check_restore, create_backup
from sleepscape.services.environment_views import EnvironmentViewService
from sleepscape.services.views import DashboardService
from sleepscape.storage.analysis import (
    AnalysisStorageError,
    analysis_available,
    migrate_analysis,
    save_results,
)
from sleepscape.storage.environment_reader import EnvironmentReader
from sleepscape.storage.reader import DashboardReader, StorageReadError

_VOLATILE = frozenset(
    {
        "fetched_at",
        "received_at",
        "last_checked_at",
        "checked_at",
        "generated_at",
        "data_as_of",
        "time_mapping_version",
        "time_index_revision",
        "anchor_ids",
    }
)


def _semantic(value):
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in sorted(value.items()) if key not in _VOLATILE}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def _fingerprint(night, environment, result):
    """Private identity and quality digest, independent of repeated fetch/read times."""
    source = {
        "night_date": result["night_date"],
        "sleep": night.get("sleep"),
        "cards": night.get("cards"),
        "stages": night.get("stages"),
        "load_comparison": night.get("load_comparison"),
        "optional_availability": night.get("optional_availability"),
        "optional_observations": night.get("optional_observations"),
        "sources": sorted((environment or {}).get("sources", []), key=lambda r: r["source_ref"]),
        "records": sorted((environment or {}).get("records", []), key=lambda r: r["record_ref"]),
        "core_ready": result["core_ready"],
        "missing": result["missing"],
        "policy_version": result["policy_version"],
        "policy": result["policy"],
        "quality_state": result["quality_state"],
        "warnings": result["warnings"],
    }
    raw = json.dumps(
        _semantic(source),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _environment(settings, reader, night, clock):
    sleep = night.get("sleep", {})
    try:
        start, end = (datetime.fromisoformat(sleep[key]) for key in ("start_at", "end_at"))
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or not timedelta(0) < end - start <= timedelta(hours=24)
        ):
            return None
        if end > clock:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    with EnvironmentReader(settings.data_dir, connection=reader.connection) as environment:
        return EnvironmentViewService(settings).timeline(start, end, reader=environment)


def evaluate_night(settings: Settings, day: date, *, now: datetime | None = None) -> dict:
    """Compute current facts in a single database read snapshot, without migration or writes."""
    if type(day) is not date or not 2000 <= day.year <= 2100:
        raise ValueError("invalid_analysis_date")
    clock = instant(now if now is not None else datetime.now(UTC))
    view = DashboardService(settings)
    view._now = lambda: clock
    try:
        with DashboardReader(settings.data_dir) as reader:
            night = view.readiness_night(reader, day)
            environment = _environment(settings, reader, night, clock)
            result = evaluate_readiness(
                day, night, environment, now=clock, lookback_days=settings.garmin_lookback_days
            )
            result["source_fingerprint"] = _fingerprint(night, environment, result)
            return result
    except (StorageReadError, AnalysisStorageError):
        raise
    except Exception:
        raise StorageReadError("analysis_readiness_unavailable") from None


def public_readiness(result: dict) -> dict:
    """The domain result is safe; storage identities and private fingerprints are not public."""
    fields = (
        "policy_version",
        "policy",
        "quality_state",
        "warnings",
        "night_date",
        "checked_at",
        "data_state",
        "core_ready",
        "missing",
        "optional_missing",
        "environment_summary",
        "garmin_summary",
        "limitations",
        "window",
    )
    return {key: result[key] for key in fields}


def prepare_readiness(settings: Settings) -> dict:
    """Explicit optional schema setup, preceded by a recoverable online SQLite backup."""
    with DashboardReader(settings.data_dir) as reader:
        if reader.connection is None:
            raise AnalysisStorageError("analysis_source_unavailable")
        present = analysis_available(reader.connection)
    if present:
        return {"status": "already_initialized", "backup_verified": False, "schema_version": 1}
    saved = create_backup(settings)
    check_restore(settings, Path(saved["backup_path"]))
    result = migrate_analysis(settings.data_dir)
    return {**result, "backup_verified": True}


def check_readiness(
    settings: Settings,
    *,
    day: date | None = None,
    now: datetime | None = None,
    persist: bool = True,
) -> dict:
    """Check one explicit night or the configured N-day window. Never collect or call models."""
    clock = instant(now if now is not None else datetime.now(UTC))
    period = window(clock, settings.garmin_lookback_days)
    if day is not None:
        days = [day]
    else:
        first, last = (date.fromisoformat(period[key]) for key in ("start_date", "end_date"))
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    if persist:
        with DashboardReader(settings.data_dir) as reader:
            if reader.connection is None or not analysis_available(reader.connection):
                raise AnalysisStorageError("analysis_not_initialized")
    results = [evaluate_night(settings, value, now=clock) for value in days]
    saved = save_results(settings.data_dir, results, run_id=str(uuid4())) if persist else None
    return {
        "status": "checked",
        "persisted": persist,
        "window": {"start_date": days[0].isoformat(), "end_date": days[-1].isoformat()},
        "counts": {
            state: sum(r["data_state"] == state for r in results)
            for state in ("ready", "waiting_data", "expired_unanalyzed")
        },
        "results": [public_readiness(result) for result in results],
        "storage": saved,
        "analysis": "skipped_deferred",
        "model_calls": 0,
    }


def next_check(settings: Settings, *, now=None) -> dict:
    return schedule(instant(now if now is not None else datetime.now(UTC)))
