"""Bounded collection, pagination, and atomic current-data replacement."""

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sleepscape.adapters.catalog import PROVISIONAL_RESOURCES, build_plan, build_sync_plan
from sleepscape.adapters.garmin import GarminError, GarminGateway
from sleepscape.config import Settings
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.domain.normalize import normalize
from sleepscape.storage.reader import StorageReadError, completed_run_plans
from sleepscape.storage.repository import Repository


class CollectionError(Exception):
    pass


def prepare_sync(
    settings: Settings, today: date, *, detail_days: int = 7, history_days: int | None = None
) -> tuple[list[DataRequest], dict]:
    """Plan from read-only run metadata; never read credentials or access Garmin."""
    if not 1 <= detail_days <= 31:
        raise ValueError("Require 1 <= detail_days <= 31")
    if history_days is not None and not detail_days <= history_days <= 366:
        raise ValueError("Require detail_days <= history_days <= 366")
    completed = []
    try:
        for plan in completed_run_plans(settings.data_dir):
            day = date.fromisoformat(plan.get("complete_day_through", plan["end_date"]))
            if day < today:
                completed.append((day, int(plan["history_days"])))
    except (StorageReadError, ValueError, TypeError, KeyError):
        raise CollectionError("sync_state_unavailable") from None
    yesterday = today - timedelta(days=1)
    latest = max((day for day, _ in completed), default=None)
    wide = max((day for day, span in completed if span >= 38), default=None)
    if latest is None:
        mode, selected_history = "initial", 38
    elif wide is None or (yesterday - wide).days >= 7:
        mode, selected_history = "weekly", 38
    else:
        mode, selected_history = "rolling", 7
    gap = max(0, (yesterday - latest).days) if latest else 0
    selected_detail = min(31, max(detail_days, gap + 1))
    if history_days is not None:
        mode, selected_history = "explicit", history_days
    selected_history = max(selected_history, selected_detail)
    metadata = {
        "mode": "sync",
        "sync_strategy": mode,
        "end_date": today.isoformat(),
        "detail_start": (today - timedelta(days=selected_detail - 1)).isoformat(),
        "history_start": (today - timedelta(days=selected_history)).isoformat(),
        "detail_days": selected_detail,
        "history_days": selected_history,
        "complete_day_through": yesterday.isoformat(),
        "provisional_dates": [today.isoformat()],
        "provisional_resources": sorted(PROVISIONAL_RESOURCES),
        "missed_complete_days": gap,
    }
    if gap > 30:
        first = latest + timedelta(days=1)
        commands = []
        while first <= yesterday:
            last = min(yesterday, first + timedelta(days=30))
            length = (last - first).days + 1
            commands.append(
                "uv run --locked sleepscape garmin-collect "
                f"--end {last.isoformat()} --detail-days {length} --history-days {length + 31}"
            )
            first = last + timedelta(days=1)
        metadata["backfill_required"] = {
            "start_date": (latest + timedelta(days=1)).isoformat(),
            "end_date": yesterday.isoformat(),
            "commands": commands,
        }
    return build_sync_plan(today, selected_detail, selected_history), metadata


@contextmanager
def collection_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    descriptor = os.open(data_dir / ".collection.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectionError("collection_already_running") from None
        yield
    finally:
        os.close(descriptor)


def collect_activities(gateway, repository: Repository, run_id: str, request: DataRequest):
    pages, signatures, activity_ids = [], set(), set()
    limit = int(request.params["limit"])
    for page_index in range(50):
        params = {**request.params, "start": page_index * limit}
        page_request = replace(
            request,
            resource="activities_page",
            key=f"{request.key}:offset={page_index * limit}",
            params=params,
        )
        result = gateway.fetch(page_request)
        payload = result.payload
        valid = isinstance(payload, list)
        repository.save_snapshot(
            run_id,
            result,
            NormalizedBatch(
                metrics={
                    "raw_only": True,
                    "item_count": len(payload) if valid else 0,
                    "payload_shape_valid": valid,
                }
            ),
        )
        if not valid:
            raise GarminError("activity_page_invalid_shape")
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if signature in signatures:
            raise GarminError("activity_pagination_no_progress")
        signatures.add(signature)
        for activity in payload:
            if not isinstance(activity, dict) or type(activity.get("activityId")) is not int:
                raise GarminError("activity_page_invalid_id")
            if activity["activityId"] in activity_ids:
                raise GarminError("activity_pagination_overlap")
            activity_ids.add(activity["activityId"])
        pages.append(payload)
        if len(payload) < limit:
            return FetchResult(
                request,
                {"pages": pages, "pagination_complete": True},
                result.fetched_at,
                result.http_status,
            )
    raise GarminError("activity_pagination_limit")


def _check_deferred(repository: Repository):
    last = repository.latest_run()
    summary = last.get("summary") if last else None
    value = summary.get("retry_not_before") if summary else None
    if value and datetime.fromisoformat(value) > datetime.now(UTC):
        raise CollectionError("retry_after_wait_still_active")


def collect(
    settings: Settings,
    end,
    detail_days=7,
    history_days=31,
    *,
    progress=None,
    gateway=None,
    plan: list[DataRequest] | None = None,
    metadata: dict | None = None,
):
    plan = build_plan(end, detail_days, history_days) if plan is None else plan
    metadata = {
        "end_date": end.isoformat(),
        "detail_start": (end - timedelta(days=detail_days - 1)).isoformat(),
        "history_start": (end - timedelta(days=history_days - 1)).isoformat(),
        "detail_days": detail_days,
        "history_days": history_days,
        "timezone": settings.timezone,
        **(metadata or {}),
        "resources": sorted({request.resource for request in plan}),
        "planned_requests": len(plan),
    }
    with collection_lock(settings.data_dir), Repository(settings.data_dir) as repository:
        _check_deferred(repository)
        run_id = repository.start_run(metadata)
        initial_snapshots = repository.counts()["raw_snapshots"]
        summary = {
            "run_id": run_id,
            "completed_resources": 0,
            "failed_resources": 0,
            "new_snapshots": 0,
            "status": "running",
            "plan": metadata,
        }
        own_gateway = gateway is None
        try:
            if gateway is None:
                gateway = GarminGateway(settings)
            for request in plan:
                try:
                    result = (
                        collect_activities(gateway, repository, run_id, request)
                        if request.resource == "activities"
                        else gateway.fetch(request)
                    )
                    try:
                        batch = normalize(request, result.payload, settings.timezone)
                    except Exception:
                        batch = NormalizedBatch(
                            issues=[
                                {
                                    "severity": "error",
                                    "code": "normalization_exception",
                                    "field": "$",
                                }
                            ],
                            metrics={"payload_shape_valid": False},
                        )
                    provisional_dates = [
                        day
                        for day in metadata.get("provisional_dates", [])
                        if request.resource in PROVISIONAL_RESOURCES
                        and request.start_date is not None
                        and request.end_date is not None
                        and request.start_date <= day <= request.end_date
                    ]
                    if metadata.get("mode") == "sync":
                        batch.metrics["provisional_dates"] = provisional_dates
                        batch.metrics["collection_state"] = (
                            "provisional" if provisional_dates else "completed_day"
                        )
                        batch.metrics["complete_day_through"] = metadata["complete_day_through"]
                    stored = repository.save_snapshot(run_id, result, batch)
                    summary["new_snapshots"] += int(stored["new_snapshot"])
                    summary["completed_resources"] += 1
                    if stored["status"] == "invalid":
                        summary["failed_resources"] += 1
                    if progress:
                        progress(
                            {
                                "resource": request.resource,
                                "status": stored["status"],
                                "records": stored["record_count"],
                            }
                        )
                except GarminError as exc:
                    repository.record_failure(run_id, request, exc.code, exc.http_status)
                    summary["failed_resources"] += 1
                    if progress:
                        progress(
                            {"resource": request.resource, "status": "failed", "code": exc.code}
                        )
                    if getattr(exc, "retry_after_seconds", None) is not None:
                        summary["retry_not_before"] = (
                            datetime.now(UTC) + timedelta(seconds=exc.retry_after_seconds)
                        ).isoformat()
                    if (
                        exc.http_status in {401, 403, 429}
                        or exc.code.startswith(("authentication_", "credential_"))
                        or exc.code
                        in {"retry_after_deferred", "authorization_failed", "empty_key_file"}
                        or getattr(gateway, "client", True) is None
                    ):
                        summary["stop_reason"] = exc.code
                        break
            summary["status"] = "partial" if summary["failed_resources"] else "completed"
        except (GarminError, CollectionError) as exc:
            summary["status"] = "failed"
            summary["stop_reason"] = getattr(exc, "code", "collection_error")
            summary["failed_resources"] += 1
        except KeyboardInterrupt:
            summary["status"] = "interrupted"
            raise
        except Exception:
            summary["status"] = "failed"
            summary["stop_reason"] = "local_collection_error"
            raise CollectionError("local_collection_error") from None
        finally:
            summary["new_snapshots"] = repository.counts()["raw_snapshots"] - initial_snapshots
            session = getattr(gateway, "session", None)
            summary["http_requests"] = getattr(session, "request_count", None)
            if own_gateway and gateway is not None:
                gateway.close()
            repository.finish_run(run_id, summary["status"], summary)
        summary["database_counts"] = repository.counts()
        summary["integrity"] = repository.integrity_check()
        return summary
