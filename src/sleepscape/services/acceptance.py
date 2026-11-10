"""Read-only source audit and disposable replay, with no account or network access.

Only counts, booleans, and static diagnostic codes leave this module. Actual
source replay and independent synthetic storage checks are reported separately.
"""

import hashlib
import json
import sqlite3
import stat
import zlib
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sleepscape.config import Settings
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.domain.normalize import normalize
from sleepscape.storage import Repository

FRESHNESS_LIMIT_SECONDS = 24 * 60 * 60
_TABLES = ("runs", "raw_snapshots", "normalized_records", "requests")
_SUCCESS = {"ok", "partial", "empty"}
_RUN_STATES = {"running", "completed", "partial", "failed", "interrupted"}


def _canonical(value) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _record_map(rows) -> dict:
    """Snapshot identifiers differ between databases; source content must not."""
    result = {}
    for row in rows:
        item = dict(row)
        if "record_key" in item:
            item["key"] = item.pop("record_key")
        if "data_json" in item:
            item["data"] = json.loads(item.pop("data_json"))
        item.pop("snapshot_id", None)
        identity = (item["resource"], item["kind"], item["key"])
        result[identity] = _canonical(item)
    return result


def _in_scope(record, requests) -> bool:
    for request in requests:
        if record["resource"] != request["resource"]:
            continue
        if record["request_key"] == request["request_key"]:
            return True
        first, last, day = request["start_date"], request["end_date"], record["date"]
        if day is not None and (first is not None or last is not None):
            if (first is None or day >= first) and (last is None or day <= last):
                return True
    return False


def _reconstruct(request, raw, timezone):
    payload = json.loads(zlib.decompress(raw["payload_zlib"]))
    data_request = DataRequest(
        request["resource"],
        request["request_key"],
        "/offline-replay",
        start_date=request["start_date"],
        end_date=request["end_date"],
    )
    batch = (
        NormalizedBatch(
            metrics={
                "raw_only": True,
                "item_count": len(payload) if isinstance(payload, list) else 0,
                "payload_shape_valid": isinstance(payload, list),
            }
        )
        if request["resource"] == "activities_page"
        else normalize(data_request, payload, timezone)
    )
    return FetchResult(data_request, payload, request["fetched_at"], request["http_status"]), batch


def _replay(source, requests, destination: Path, timezone: str) -> dict:
    snapshots = {}
    for request in requests:
        snapshot_id = request["snapshot_id"]
        if snapshot_id not in snapshots:
            snapshot = source.execute(
                "SELECT * FROM raw_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if snapshot is None:
                raise ValueError("missing_snapshot")
            snapshots[snapshot_id] = dict(snapshot)
    source_rows = [
        row
        for row in source.execute("SELECT * FROM normalized_records")
        if _in_scope(row, requests)
    ]
    expected = _record_map(source_rows)
    invalid_batches = 0
    request_status_mismatches = 0
    request_count_mismatches = 0
    version_changes = 0
    references_match = True
    with Repository(destination) as shadow:
        first_run = shadow.start_run({"purpose": "source_replay"})
        for request in requests:
            raw = snapshots[request["snapshot_id"]]
            references_match &= (
                raw["resource"] == request["resource"]
                and raw["request_key"] == request["request_key"]
            )
            result, batch = _reconstruct(request, raw, timezone)
            if request["resource"] != "activities_page":
                previous_version = json.loads(request["metrics_json"]).get("normalization_version")
                version_changes += previous_version != batch.metrics.get("normalization_version")
            stored = shadow.save_snapshot(first_run, result, batch)
            invalid_batches += stored["status"] == "invalid"
            request_status_mismatches += stored["status"] != request["status"]
            request_count_mismatches += stored["record_count"] != request["record_count"]
        first_records = _record_map(shadow.list_records())
        first_counts = shadow.counts()
        second_run = shadow.start_run({"purpose": "repeated_source_replay"})
        # Reuse the reconstructed requests, not their raw payloads as normalized records.
        for request in requests:
            result, batch = _reconstruct(request, snapshots[request["snapshot_id"]], timezone)
            shadow.save_snapshot(second_run, result, batch)
        repeated_records = _record_map(shadow.list_records())
        second_counts = shadow.counts()
        different = sum(first_records.get(key) != value for key, value in expected.items())
        unexpected = len(first_records.keys() - expected.keys())
        missing = len(expected.keys() - first_records.keys())
        content_differences = metadata_differences = 0
        for key in expected.keys() & first_records.keys():
            source_record = json.loads(expected[key])
            replayed_record = json.loads(first_records[key])
            source_metadata = [source_record.pop(field) for field in ("request_key", "fetched_at")]
            replayed_metadata = [
                replayed_record.pop(field) for field in ("request_key", "fetched_at")
            ]
            metadata_differences += source_metadata != replayed_metadata
            content_differences += source_record != replayed_record
        result = {
            "performed": True,
            "successful_requests_replayed": len(requests),
            "raw_only_requests": sum(r["resource"] == "activities_page" for r in requests),
            "source_unique_snapshots": len(snapshots),
            "first_import_snapshots": first_counts["raw_snapshots"],
            "repeat_import_snapshots": second_counts["raw_snapshots"],
            "snapshot_references_match": references_match,
            "snapshots_deduplicated": (
                len(snapshots) == first_counts["raw_snapshots"] == second_counts["raw_snapshots"]
            ),
            "source_scoped_records": len(expected),
            "current_records_outside_replay_scope": (
                source.execute("SELECT COUNT(*) FROM normalized_records").fetchone()[0]
                - len(expected)
            ),
            "first_import_records": len(first_records),
            "repeat_import_records": len(repeated_records),
            "source_record_mismatches": different,
            "normalization_content_mismatches": content_differences,
            "provenance_metadata_mismatches": metadata_differences,
            "missing_replayed_records": missing,
            "unexpected_replayed_records": unexpected,
            "all_normalized_records_match": expected == first_records,
            "repeat_current_records_stable": first_records == repeated_records,
            "invalid_normalized_batches": invalid_batches,
            "request_status_mismatches": request_status_mismatches,
            "request_record_count_mismatches": request_count_mismatches,
            "normalizer_version_changed_requests": version_changes,
            "shadow_integrity_ok": shadow.integrity_check() == ["ok"],
        }
    return result


def _synthetic_checks(destination: Path) -> dict:
    request = DataRequest("synthetic", "synthetic", "/offline", start_date="2000-01-01")

    def record(key, version):
        return {
            "kind": "synthetic",
            "key": key,
            "date": "2000-01-01",
            "timestamp": None,
            "data": {"version": version},
        }

    with Repository(destination) as repository:
        run = repository.start_run({"purpose": "synthetic_storage_checks"})
        original = [record("a", 1), record("b", 1)]
        repository.save_snapshot(
            run,
            FetchResult(request, original, "2000-01-01T00:00:00Z", 200),
            NormalizedBatch(original),
        )
        revision = [record("a", 2)]
        repository.save_snapshot(
            run,
            FetchResult(request, revision, "2000-01-02T00:00:00Z", 200),
            NormalizedBatch(revision),
        )
        rows = repository.list_records()
        revision_ok = len(rows) == 1 and rows[0]["data"] == {"version": 2}
        audit_ok = repository.counts()["raw_snapshots"] == 2
        before = _record_map(rows)
        repository.record_failure(run, request, "synthetic_failure", 503)
        failed_retained = _record_map(repository.list_records()) == before
        repository.save_snapshot(
            run,
            FetchResult(request, {"synthetic_invalid": True}, "2000-01-03T00:00:00Z", 200),
            NormalizedBatch(metrics={"payload_shape_valid": False}),
        )
        invalid_retained = _record_map(repository.list_records()) == before
        counts = repository.counts()
        try:
            repository.save_snapshot(
                run,
                FetchResult(request, {"synthetic_rollback": True}, "2000-01-04T00:00:00Z", 200),
                NormalizedBatch([{"kind": "synthetic"}]),
            )
        except KeyError:
            rollback_ok = (
                repository.counts() == counts and _record_map(repository.list_records()) == before
            )
        else:
            rollback_ok = False
        repository.save_snapshot(
            run,
            FetchResult(request, [], "2000-01-05T00:00:00Z", 200),
            NormalizedBatch(),
        )
        return {
            "revision_replaces_and_deletes_current": revision_ok,
            "revision_preserves_raw_history": audit_ok,
            "fetch_failure_preserves_current": failed_retained,
            "invalid_payload_preserves_current": invalid_retained,
            "transaction_failure_rolls_back": rollback_ok,
            "valid_empty_clears_scope": repository.list_records() == [],
            "integrity_ok": repository.integrity_check() == ["ok"],
        }


def _read_source(path: Path, settings: Settings, temporary: Path) -> tuple[dict, dict, list[str]]:
    errors = []
    source_info = {}
    # mode=ro forbids writes to the source database, including schema and journal changes.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only = ON")
        source.execute("BEGIN")
        integrity = [row[0] for row in source.execute("PRAGMA integrity_check")]
        source_info["integrity_ok"] = integrity == ["ok"]
        source_info["foreign_key_violations"] = len(
            source.execute("PRAGMA foreign_key_check").fetchall()
        )
        source_info["schema_version"] = source.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        source_info["required_schema_present"] = all(table in tables for table in _TABLES)
        if not source_info["required_schema_present"]:
            return source_info, {"performed": False}, ["unsupported_source_schema"]
        source_info["counts"] = {
            table: source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _TABLES
        }
        raw_bytes = raw_errors = 0
        for snapshot in source.execute("SELECT content_hash, payload_zlib FROM raw_snapshots"):
            try:
                raw = zlib.decompress(snapshot["payload_zlib"])
                raw_bytes += len(raw)
                json.loads(raw)
                raw_errors += hashlib.sha256(raw).hexdigest() != snapshot["content_hash"]
            except (ValueError, TypeError, zlib.error):
                raw_errors += 1
        source_info["raw_uncompressed_bytes"] = raw_bytes
        source_info["raw_hash_or_json_failures"] = raw_errors
        if raw_errors:
            return source_info, {"performed": False}, ["raw_snapshot_integrity_failed"]
        latest = source.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        completed = source.execute(
            "SELECT * FROM runs WHERE status = 'completed' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        source_info["latest_run_complete"] = bool(latest and latest["status"] == "completed")
        source_info["latest_complete_run_is_latest"] = bool(
            latest and completed and latest["id"] == completed["id"]
        )
        source_info["freshness_limit_seconds"] = FRESHNESS_LIMIT_SECONDS
        source_info["latest_run_fresh"] = False
        source_info["latest_run_age_seconds"] = None
        source_info["latest_run_status"] = (
            latest["status"] if latest and latest["status"] in _RUN_STATES else "unavailable"
        )
        if latest:
            try:
                instant = datetime.fromisoformat(latest["finished_at"] or latest["started_at"])
                if instant.tzinfo is None:
                    raise ValueError("timestamp_without_timezone")
                age = (datetime.now(UTC) - instant).total_seconds()
                source_info["latest_run_age_seconds"] = round(age, 3)
                source_info["latest_run_fresh"] = 0 <= age <= FRESHNESS_LIMIT_SECONDS
            except (ValueError, TypeError):
                errors.append("invalid_run_timestamp")
        if completed is None:
            return source_info, {"performed": False}, errors + ["no_completed_collection_run"]
        requests = [
            dict(row)
            for row in source.execute(
                "SELECT * FROM requests WHERE run_id = ? ORDER BY id", (completed["id"],)
            )
        ]
        successful = [row for row in requests if row["status"] in _SUCCESS]
        source_info["selected_run_request_count"] = len(requests)
        source_info["selected_run_unsuccessful_requests"] = len(requests) - len(successful)
        run_plan = json.loads(completed["plan_json"])
        expected = run_plan.get("planned_requests")
        main_count = sum(row["resource"] != "activities_page" for row in successful)
        source_info["selected_run_plan_count_known"] = type(expected) is int
        source_info["selected_run_plan_fully_recorded"] = (
            type(expected) is int and main_count == expected
        )
        if not successful:
            return source_info, {"performed": False}, errors + ["no_successful_requests_to_replay"]
        result = _replay(
            source, successful, temporary / "replay", run_plan.get("timezone", settings.timezone)
        )
    return source_info, result, errors


def _audit(settings: Settings) -> dict:
    report = {
        "status": "needs_attention",
        "source": {},
        "data_replay": {"performed": False},
        "synthetic_checks": {},
        "errors": [],
    }
    path = settings.data_dir / "sleepscape.sqlite3"
    with TemporaryDirectory(prefix="sleepscape-acceptance-") as directory:
        temporary = Path(directory)
        temporary.chmod(0o700)
        try:
            report["synthetic_checks"] = _synthetic_checks(temporary / "synthetic")
            if not path.is_file():
                report["errors"].append("source_database_missing")
            else:
                source, replay, errors = _read_source(path, settings, temporary)
                report["source"], report["data_replay"] = source, replay
                report["errors"].extend(errors)
                source["database_file_bytes"] = path.stat().st_size
                source["database_and_sidecar_bytes"] = sum(
                    file.stat().st_size
                    for file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
                    if file.is_file()
                )
        except Exception:
            # Exceptions may embed source data or identifiers; never return their messages.
            report["errors"].append("offline_audit_failed")
        report["temporary_files_private"] = all(
            stat.S_IMODE(file.stat().st_mode) == (0o700 if file.is_dir() else 0o600)
            for file in [temporary, *temporary.rglob("*")]
        )
    report["temporary_files_removed"] = not temporary.exists()
    source, replay = report["source"], report["data_replay"]
    passed = (
        not report["errors"]
        and source.get("integrity_ok")
        and source.get("foreign_key_violations") == 0
        and source.get("raw_hash_or_json_failures") == 0
        and source.get("latest_complete_run_is_latest")
        and source.get("latest_run_fresh")
        and source.get("selected_run_plan_fully_recorded")
        and source.get("selected_run_unsuccessful_requests") == 0
        and replay.get("performed")
        and replay.get("snapshot_references_match")
        and replay.get("snapshots_deduplicated")
        and replay.get("all_normalized_records_match")
        and replay.get("repeat_current_records_stable")
        and replay.get("shadow_integrity_ok")
        and replay.get("invalid_normalized_batches") == 0
        and replay.get("request_status_mismatches") == 0
        and replay.get("request_record_count_mismatches") == 0
        and all(report["synthetic_checks"].values())
        and report["temporary_files_private"]
        and report["temporary_files_removed"]
    )
    report["status"] = "passed" if passed else "needs_attention"
    return report


def audit(settings: Settings) -> dict:
    """Return a safe audit report; even infrastructure failures expose no source values."""
    try:
        return _audit(settings)
    except Exception:
        # Include temporary-directory setup, permission checks, and cleanup failures.
        return {
            "status": "needs_attention",
            "source": {},
            "data_replay": {"performed": False},
            "synthetic_checks": {},
            "temporary_files_private": False,
            "temporary_files_removed": False,
            "errors": ["offline_audit_failed"],
        }
