#!/usr/bin/env python3
"""Reproducible synthetic environment capacity check; the temporary database is removed.

Run explicitly from the repository root:
    uv run --locked python tools/verify_environment_capacity.py

This measures local query/index capacity, not cloud performance or real sampling.
Only synthetic fixtures are read. No user settings, credentials or database are used.
"""

import argparse
import hashlib
import json
import resource
import sqlite3
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile, canonical_json
from sleepscape.storage import environment
from sleepscape.storage.environment_index import migrate_index, rebuild_index
from sleepscape.storage.environment_reader import WINDOW_SQL, EnvironmentReader, instant_us
from sleepscape.storage.reports import write_private_json

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2025, 1, 1, tzinfo=UTC)


def _json_hash(value: dict) -> tuple[str, str]:
    encoded = canonical_json(value)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _space(directory: Path) -> dict:
    result = {}
    for name, suffix in (("database_bytes", ""), ("wal_bytes", "-wal"), ("shm_bytes", "-shm")):
        path = directory / ("sleepscape.sqlite3" + suffix)
        result[name] = path.stat().st_size if path.exists() else 0
    return result


def _seed(directory: Path, days: int, single_boot: bool) -> None:
    fixtures = ROOT / "tests/fixtures"
    profile = MeasurementProfile.model_validate_json(
        (ROOT / "firmware/profile.example.json").read_bytes()
    ).model_copy(update={"placement_id": "synthetic-bedside"})
    template = json.loads((fixtures / "environment-batch.json").read_bytes())
    environment.register_local(directory, "synthetic-device", "synthetic-profile", profile)
    with closing(sqlite3.connect(directory / "sleepscape.sqlite3")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for day in range(days):
            instant = (START + timedelta(days=day)).isoformat().replace("+00:00", "Z")
            boot = "synthetic-continuous" if single_boot else f"synthetic-day-{day}"
            offset_minutes = day * 1440 if single_boot else 0
            anchor = {
                **template["time_anchors"][0],
                "anchor_id": str(day),
                "monotonic_ms": offset_minutes * 60000,
                "utc_at": instant,
            }
            text, digest = _json_hash(anchor)
            connection.execute(
                "INSERT INTO environment_time_anchors VALUES (?,?,?,?,?,?,?,?)",
                (
                    "synthetic-device",
                    boot,
                    anchor["anchor_id"],
                    anchor["monotonic_ms"],
                    instant,
                    text,
                    digest,
                    instant,
                ),
            )
            pending = []
            for minute in range(1440):
                record = {
                    **template["records"][0],
                    "seq": offset_minutes + minute,
                    "mono_start_ms": (offset_minutes + minute) * 60000,
                    "mono_end_ms": (offset_minutes + minute + 1) * 60000,
                }
                text, digest = _json_hash(record)
                pending.append(
                    (
                        "synthetic-device",
                        boot,
                        "minute",
                        record["seq"],
                        "synthetic-profile",
                        record["mono_start_ms"],
                        record["mono_end_ms"],
                        text,
                        digest,
                        instant,
                    )
                )
            connection.executemany(
                "INSERT INTO environment_records VALUES (?,?,?,?,?,?,?,?,?,?)", pending
            )
            connection.commit()


def _query(directory: Path, start: datetime, end: datetime) -> dict:
    callbacks = 0

    def progress():
        nonlocal callbacks
        callbacks += 1
        return 0

    with EnvironmentReader(directory) as reader:
        assert reader.connection is not None
        reader.connection.set_progress_handler(progress, 1000)
        started = time.perf_counter()
        result = reader.timeline(start, end)
        elapsed = time.perf_counter() - started
        reader.connection.set_progress_handler(None, 0)
    expected = int((end - start).total_seconds() // 60)
    if result["state"] != "available" or result["truncated"] or len(result["records"]) != expected:
        raise ValueError("capacity_query_count_mismatch")
    return {
        "record_count": expected,
        "elapsed_seconds": round(elapsed, 6),
        "sqlite_vm_steps_approx": callbacks * 1000,
    }


def _single_boot_rebuilds(directory: Path, days: int) -> dict:
    started = time.perf_counter()
    normal = rebuild_index(directory, force=True)
    normal_seconds = time.perf_counter() - started
    conflict = EnvironmentBatch.model_validate(
        {
            "schema_version": 1,
            "device_id": "synthetic-device",
            "boot_id": "synthetic-continuous",
            "records": [],
            "time_anchors": [
                {
                    "anchor_id": "synthetic-conflict",
                    "monotonic_ms": days * 86400000,
                    "utc_at": (START + timedelta(days=days, minutes=5))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "source": "ntp",
                    "uncertainty_ms": None,
                    "quality": [],
                }
            ],
        }
    )
    started = time.perf_counter()
    environment.ingest(directory, "synthetic-device", conflict)
    conflict_seconds = time.perf_counter() - started
    with closing(sqlite3.connect(directory / "sleepscape.sqlite3")) as connection:
        conflicts = connection.execute(
            "SELECT COUNT(*) FROM environment_time_index "
            "WHERE time_quality='conflict' AND utc_start_us IS NULL AND utc_end_us IS NULL"
        ).fetchone()[0]
        current = connection.execute(
            "SELECT source_revision=indexed_revision FROM environment_index_boots"
        ).fetchall()
    if conflicts != days * 1440 or current != [(1,)]:
        raise ValueError("capacity_conflict_rebuild_incomplete")
    return {
        "normal_force_seconds": round(normal_seconds, 6),
        "normal_record_count": normal["record_count"],
        "conflict_ingest_rebuild_seconds": round(conflict_seconds, 6),
        "conflict_record_count": conflicts,
        "conflict_utc_all_null": True,
        "index_current": True,
    }


def verify_capacity(days: int = 365, single_boot: bool = False) -> dict:
    if not 2 <= days <= 366:
        raise ValueError("capacity_days_requires_2_to_366")
    started, cpu_started = time.perf_counter(), time.process_time()
    with TemporaryDirectory(prefix="sleepscape-environment-capacity-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        _seed(directory, days, single_boot)
        seed_seconds = time.perf_counter() - started
        before = _space(directory)
        index_started = time.perf_counter()
        migrate_index(directory)
        indexed = rebuild_index(directory)
        index_seconds = time.perf_counter() - index_started
        after = _space(directory)
        with closing(sqlite3.connect(directory / "sleepscape.sqlite3")) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            count = connection.execute("SELECT COUNT(*) FROM environment_time_index").fetchone()[0]
            start = START + timedelta(days=days - 2)
            end = start + timedelta(hours=8)
            plan = [
                row[3]
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN " + WINDOW_SQL,
                    (instant_us(start) - 60000000, instant_us(end), instant_us(start), 10001),
                )
            ]
        checkpointed = _space(directory)
        if count != days * 1440:
            raise ValueError("capacity_index_count_mismatch")
        uses_index = any(
            "SEARCH t USING" in step and "environment_time_index_utc" in step for step in plan
        )
        if not uses_index or any("SCAN t" in step or "SCAN r" in step for step in plan):
            raise ValueError("capacity_window_index_not_used")
        queries = {
            "eight_hours": _query(directory, start, start + timedelta(hours=8)),
            "forty_eight_hours": _query(directory, start, start + timedelta(hours=48)),
        }
        month_started = time.perf_counter()
        month_records = 0
        for day in range(min(days, 31)):
            start = START + timedelta(days=days - 1 - day)
            month_records += _query(directory, start, start + timedelta(days=1))["record_count"]
        month_seconds = time.perf_counter() - month_started
        # A scan of all 525600 facts would substantially exceed these bounded-window budgets.
        bounded = (
            queries["eight_hours"]["sqlite_vm_steps_approx"] < 200000
            and queries["forty_eight_hours"]["sqlite_vm_steps_approx"] < 1000000
        )
        if not bounded:
            raise ValueError("capacity_query_work_exceeded")
        continuous = _single_boot_rebuilds(directory, days) if single_boot else None
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_rss_bytes = int(peak_rss if sys.platform == "darwin" else peak_rss * 1024)
        report = {
            "status": "verified",
            "synthetic": True,
            "local_machine_only": True,
            "days": days,
            "single_boot": single_boot,
            "minute_record_count": count,
            "seed_seconds": round(seed_seconds, 3),
            "index_seconds": round(index_seconds, 3),
            "cpu_seconds": round(time.process_time() - cpu_started, 3),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "peak_rss_bytes": peak_rss_bytes,
            "space_before_index": before,
            "space_after_index_before_checkpoint": after,
            "space_after_checkpoint": checkpointed,
            "space_after_rebuild_checks": _space(directory),
            "query_plan": plan,
            "bounded_window_index_used": uses_index,
            "bounded_query_work": bounded,
            "queries": queries,
            "month_daily_windows": {
                "window_count": min(days, 31),
                "record_count": month_records,
                "elapsed_seconds": round(month_seconds, 6),
            },
            "index_summary": indexed,
            "continuous_boot_rebuilds": continuous,
        }
    report["temporary_database_removed"] = not directory.exists()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument(
        "--single-boot",
        action="store_true",
        help="One boot with normal and conflict rebuild checks",
    )
    parser.add_argument("--output", type=Path, help="Optional synthetic summary JSON destination")
    args = parser.parse_args()
    try:
        result = verify_capacity(args.days, args.single_boot)
        if args.output:
            write_private_json(args.output, result)
    except Exception:
        result = {"status": "failed", "code": "environment_capacity_verification_failed"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
