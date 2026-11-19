"""Synthetic readiness audit, explicit migration and failure protection."""

import copy
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import date
from uuid import uuid4

import pytest
import requests

from sleepscape.storage import analysis
from sleepscape.storage.analysis import AnalysisStorageError
from sleepscape.storage.repository import Repository
from sleepscape.storage.usage import CapacityError


@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(key)

    def forbidden(*args, **kwargs):
        pytest.fail("Readiness storage tests must not access a network")

    monkeypatch.setattr(requests.Session, "send", forbidden)


@pytest.fixture
def data_dir(tmp_path):
    directory = tmp_path / "data"
    with Repository(directory):
        pass
    return directory


def ready(day="2001-01-02", checked_at="2001-01-02T10:00:00Z"):
    return {
        "policy_version": "dual-source-strict-1",
        "night_date": day,
        "checked_at": checked_at,
        "data_state": "ready",
        "core_ready": True,
        "window": {
            "start": f"{day}T00:00:00Z",
            "end": f"{day}T08:00:00Z",
            "duration_seconds": 28800,
        },
        "missing": [],
        "optional_missing": [],
        "garmin_summary": {
            "required_count": 5,
            "available_count": 5,
            "window_state": "available",
            "metrics": {},
        },
        "environment_summary": {"expected_windows": 480, "observed_windows": 480, "channels": {}},
        "limitations": ["clock_error_unknown"],
        "source_fingerprint": "a" * 64,
    }


def waiting():
    value = ready()
    value.update(data_state="waiting_data", core_ready=False)
    value["missing"] = [
        {
            "code": "missing_garmin_core",
            "source": "garmin",
            "metric": "sleep.night_average_hrv",
            "date": "2001-01-02",
            "window_start": None,
            "window_end": None,
            "expected": 1,
            "observed": 0,
            "recoverability": "await_sync",
            "last_checked_at": value["checked_at"],
        }
    ]
    return value


def coverage_ready(*, with_gaps=False):
    """Current contract; strict fixtures above deliberately retain historical bytes."""
    value = ready(checked_at="2001-01-02T12:00:00Z")
    value.update(
        policy_version="dual-source-coverage-1",
        policy={"min_channel_coverage": 0.99, "max_affected_span_seconds": 300},
        quality_state="with_gaps" if with_gaps else "complete",
        warnings=[],
    )
    value["garmin_summary"]["metrics"] = {
        key: {"state": "available", "source_date": "2001-01-01" if previous else "2001-01-02"}
        for key, previous in (
            ("sleep_seconds", False),
            ("average_heart_rate", False),
            ("night_average_hrv", False),
            ("training_load", True),
            ("previous_stress", True),
        )
    }
    channels = {}
    for metric in ("temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative"):
        count = 2880 if metric in {"temperature_c", "relative_humidity_pct"} else 28800
        channels[metric] = {
            "expected_windows": 480,
            "valid_windows": 480,
            "invalid_windows": 0,
            "expected_count": count,
            "observed_count": count,
            "valid_count": count,
            "missing_count": 0,
            "coverage_ratio": 1,
            "missing_seconds": 0,
            "effective_coverage_ratio": 1,
            "usable_count": count,
            "max_affected_span_seconds": 0,
        }
    channels["sound_relative"].update(
        audio_missing_seconds=0, audio_missing_scope="whole_observed_minutes"
    )
    value["environment_summary"] = {
        "expected_windows": 480,
        "observed_windows": 480,
        "missing_windows": 0,
        "missing_windows_lower_bound": 0,
        "count_exact": True,
        "expected_seconds": 28800,
        "covered_seconds": 28800,
        "missing_seconds": 0,
        "overlap_seconds": 0,
        "gaps": [],
        "channels": channels,
        "coverage_scope": "fully_valid_windows_clipped_to_sleep",
    }
    if with_gaps:
        channels["temperature_c"].update(
            valid_windows=479,
            invalid_windows=1,
            valid_count=2879,
            missing_count=1,
            coverage_ratio=479 / 480,
            missing_seconds=60,
            effective_coverage_ratio=2879 / 2880,
            usable_count=2879,
            max_affected_span_seconds=60,
        )
        value["warnings"] = [
            {
                "code": "invalid_channel",
                "source": "environment",
                "metric": "temperature_c",
                "date": value["night_date"],
                "window_start": "2001-01-02T01:00:00Z",
                "window_end": "2001-01-02T01:01:00Z",
                "expected": "complete_window",
                "observed": "counts_incomplete",
                "recoverability": "unrecoverable",
                "last_checked_at": value["checked_at"],
            }
        ]
    return value


def rows(data_dir, table):
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection:
        return connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def test_coverage_contract_preserves_strict_audit_bytes_hashes_and_schema(data_dir):
    analysis.migrate_analysis(data_dir)
    old_run = str(uuid4())
    old = ready()
    other = ready(day="2001-01-03", checked_at="2001-01-03T10:00:00Z")
    analysis.save_results(data_dir, [old, other], run_id=old_run)
    original = rows(data_dir, "analysis_readiness_checks")
    schema = rows(data_dir, "analysis_schema")
    current = coverage_ready(with_gaps=True)
    run = str(uuid4())
    analysis.save_results(data_dir, [current], run_id=run)
    assert rows(data_dir, "analysis_readiness_checks")[:2] == original
    assert analysis.read_latest(data_dir, old["night_date"]) == {
        **current,
        "check_id": 3,
        "run_id": run,
    }
    archived = analysis.read_latest(data_dir, other["night_date"])
    assert archived == {**other, "check_id": 2, "run_id": old_run}
    assert not {"policy", "quality_state", "warnings"} & archived.keys()
    assert analysis.migrate_analysis(data_dir)["status"] == "already_initialized"
    assert rows(data_dir, "analysis_schema") == schema
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection:
        analysis.validate_analysis(connection)


@pytest.mark.parametrize("quality", ["complete", "with_gaps", "insufficient"])
@pytest.mark.parametrize("expired", [False, True])
def test_coverage_quality_and_expiry_are_independent(data_dir, quality, expired):
    analysis.migrate_analysis(data_dir)
    value = coverage_ready(with_gaps=quality == "with_gaps")
    if quality == "insufficient":
        value.update(quality_state=quality, core_ready=False, data_state="waiting_data")
        value["missing"] = waiting()["missing"]
        # Unknown source/count metadata is retained as unknown, never coerced to zero.
        value["environment_summary"]["channels"]["temperature_c"] = None
        sound = value["environment_summary"]["channels"]["sound_relative"]
        sound.update(expected_count=None, effective_coverage_ratio=None, audio_missing_seconds=None)
    if expired:
        value["data_state"] = "expired_unanalyzed"
    analysis.save_results(data_dir, [value], run_id=str(uuid4()))
    read = analysis.read_latest(data_dir, value["night_date"])
    assert read["quality_state"] == quality
    assert read["core_ready"] is (quality != "insufficient")
    assert read["data_state"] == value["data_state"]


@pytest.mark.parametrize(
    "corruption",
    [
        "unknown_policy",
        "strict_with_new_fields",
        "missing_policy",
        "missing_warnings",
        "missing_quality",
        "wrong_minimum",
        "wrong_span",
        "extra_policy",
        "false_complete",
        "missing_blockers",
        "garmin_warning",
        "manual_warning",
        "below_threshold",
        "long_gap",
        "unknown_channel_field",
        "negative_usable",
        "ratio_above_one",
        "missing_expected_ratio",
        "unknown_summary_field",
        "unknown_metric_field",
        "unknown_channel",
        "null_ready_channel",
    ],
)
def test_malformed_coverage_batch_is_rejected_without_partial_audit(data_dir, corruption):
    analysis.migrate_analysis(data_dir)
    value = coverage_ready(with_gaps=True)
    channel = value["environment_summary"]["channels"]["temperature_c"]
    if corruption == "unknown_policy":
        value["policy_version"] = "dual-source-future-1"
    elif corruption == "strict_with_new_fields":
        value["policy_version"] = "dual-source-strict-1"
    elif corruption.startswith("missing_") and corruption != "missing_blockers":
        if corruption == "missing_expected_ratio":
            channel["expected_count"] = None
        else:
            del value[
                "quality_state"
                if corruption == "missing_quality"
                else corruption.removeprefix("missing_")
            ]
    elif corruption == "wrong_minimum":
        value["policy"]["min_channel_coverage"] = 0.98
    elif corruption == "wrong_span":
        value["policy"]["max_affected_span_seconds"] = 301
    elif corruption == "extra_policy":
        value["policy"]["private_text"] = "not_allowed"
    elif corruption == "false_complete":
        value["quality_state"] = "complete"
    elif corruption == "missing_blockers":
        value.update(quality_state="insufficient", core_ready=False, data_state="waiting_data")
    elif corruption in {"garmin_warning", "manual_warning"}:
        value["warnings"][0]["source"] = corruption.removesuffix("_warning")
    elif corruption == "below_threshold":
        channel["effective_coverage_ratio"] = 0.989
    elif corruption == "long_gap":
        channel["max_affected_span_seconds"] = 300.1
    elif corruption == "unknown_channel_field":
        channel["raw_private_text"] = "not_allowed"
    elif corruption == "negative_usable":
        channel["usable_count"] = -1
    elif corruption == "ratio_above_one":
        channel["effective_coverage_ratio"] = 1.01
    elif corruption == "unknown_summary_field":
        value["environment_summary"]["raw_private_text"] = "not_allowed"
    elif corruption == "unknown_metric_field":
        value["garmin_summary"]["metrics"]["sleep_seconds"]["raw_private_text"] = "not_allowed"
    elif corruption == "unknown_channel":
        value["environment_summary"]["channels"]["unapproved_sensor"] = copy.deepcopy(channel)
    else:
        value["environment_summary"]["channels"]["temperature_c"] = None
    valid = ready(day="2001-01-03", checked_at="2001-01-03T10:00:00Z")
    with pytest.raises(AnalysisStorageError, match="^analysis_payload_invalid$"):
        analysis.save_results(data_dir, [valid, value], run_id=str(uuid4()))
    assert rows(data_dir, "analysis_readiness_checks") == []
    assert rows(data_dir, "analysis_readiness") == []


def test_current_domain_missing_data_result_can_be_persisted_without_a_window(data_dir):
    from datetime import UTC, datetime

    from sleepscape.domain.analysis_readiness import evaluate_readiness

    analysis.migrate_analysis(data_dir)
    value = evaluate_readiness(
        date(2001, 1, 2), None, None, now=datetime(2001, 1, 2, 12, tzinfo=UTC)
    )
    value["source_fingerprint"] = "c" * 64
    analysis.save_results(data_dir, [value], run_id=str(uuid4()))
    read = analysis.read_latest(data_dir, "2001-01-02")
    assert read["policy_version"] == "dual-source-coverage-1"
    assert read["quality_state"] == "insufficient"
    assert read["window"] is read["environment_summary"] is None


@pytest.mark.parametrize(
    "changed",
    [
        {"usable_count": 0},
        {"usable_count": 2880},
        {"valid_count": 2878},
        {"observed_count": 2878},
        {"observed_count": 2881},
        {"expected_count": 0},
        {"effective_coverage_ratio": 1},
        {"missing_count": 0},
    ],
)
def test_eligible_coverage_requires_consistent_summary_counters(data_dir, changed):
    analysis.migrate_analysis(data_dir)
    value = coverage_ready(with_gaps=True)
    value["environment_summary"]["channels"]["temperature_c"].update(changed)
    with pytest.raises(AnalysisStorageError, match="^analysis_payload_invalid$"):
        analysis.save_results(data_dir, [value], run_id=str(uuid4()))
    assert rows(data_dir, "analysis_readiness_checks") == []


def test_rehashed_eligible_summary_with_inconsistent_coverage_is_rejected_on_read_and_restore(
    data_dir,
):
    analysis.migrate_analysis(data_dir)
    value = coverage_ready(with_gaps=True)
    analysis.save_results(data_dir, [value], run_id=str(uuid4()))
    value["environment_summary"]["channels"]["temperature_c"]["usable_count"] = 0
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection, connection:
        connection.execute("DROP TRIGGER analysis_readiness_checks_no_update")
        connection.execute(
            "UPDATE analysis_readiness_checks SET payload_json=?,payload_hash=?",
            (encoded, digest),
        )
        connection.execute(analysis._DDL["analysis_readiness_checks_no_update"])
    with pytest.raises(AnalysisStorageError, match="^analysis_payload_invalid$"):
        analysis.read_latest(data_dir, value["night_date"])
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection:
        with pytest.raises(AnalysisStorageError, match="^analysis_payload_invalid$"):
            analysis.validate_analysis(connection)


def test_ineligible_audit_retains_invalid_source_counts_for_diagnosis(data_dir):
    analysis.migrate_analysis(data_dir)
    value = coverage_ready()
    value.update(core_ready=False, data_state="waiting_data", quality_state="insufficient")
    issue = waiting()["missing"][0]
    value["missing"] = [
        {
            **issue,
            "code": "invalid_channel",
            "source": "environment",
            "metric": "temperature_c",
            "observed": "invalid",
            "recoverability": "unrecoverable",
        }
    ]
    channel = value["environment_summary"]["channels"]["temperature_c"]
    channel.update(
        valid_count=3000, observed_count=2900, usable_count=0, effective_coverage_ratio=0
    )
    analysis.save_results(data_dir, [value], run_id=str(uuid4()))
    saved = analysis.read_latest(data_dir, value["night_date"])
    assert saved["environment_summary"]["channels"]["temperature_c"] == channel
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection:
        analysis.validate_analysis(connection)


def test_missing_reads_never_create_database_directory_or_migrate(tmp_path, data_dir):
    missing = tmp_path / "absent"
    assert analysis.read_latest(missing, "2001-01-02") is None
    assert analysis.read_bulk(missing, [date(2001, 1, 2)]) == {}
    assert not missing.exists()
    assert analysis.read_latest(data_dir, "2001-01-02") is None
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection:
        assert not analysis.analysis_available(connection)
    with pytest.raises(AnalysisStorageError, match="analysis_source_unavailable"):
        analysis.migrate_analysis(missing)
    assert not missing.exists()


def test_migration_is_idempotent_private_and_preserves_existing_facts(data_dir):
    with Repository(data_dir) as repository:
        run_id = repository.start_run({"synthetic": True})
        repository.finish_run(run_id, "completed", {"status": "completed"})
    before = {table: rows(data_dir, table) for table in analysis._BASE_TABLES}
    assert analysis.migrate_analysis(data_dir) == {"status": "initialized", "schema_version": 1}
    schema = rows(data_dir, "analysis_schema")
    assert analysis.migrate_analysis(data_dir)["status"] == "already_initialized"
    assert rows(data_dir, "analysis_schema") == schema
    assert {table: rows(data_dir, table) for table in analysis._BASE_TABLES} == before
    assert stat.S_IMODE((data_dir / "sleepscape.sqlite3").stat().st_mode) == 0o600


def test_failed_migration_rolls_back_every_new_object(data_dir, monkeypatch):
    monkeypatch.setitem(analysis._DDL, "synthetic_failure", "INVALID PRIVATE SQL")
    with pytest.raises(AnalysisStorageError, match="^analysis_migration_failed$"):
        analysis.migrate_analysis(data_dir)
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'analysis_%'"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize("corruption", ["incomplete", "unsupported", "columns", "guard"])
def test_broken_schema_is_not_repaired_by_read_or_migration(data_dir, corruption):
    analysis.migrate_analysis(data_dir)
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection, connection:
        if corruption == "incomplete":
            connection.execute("DROP TABLE analysis_readiness")
        elif corruption == "unsupported":
            connection.execute("UPDATE analysis_schema SET version=2")
        elif corruption == "columns":
            connection.execute("ALTER TABLE analysis_readiness ADD COLUMN private_note TEXT")
        else:
            connection.execute("DROP TRIGGER analysis_readiness_checks_no_update")
    for call in (
        lambda: analysis.read_latest(data_dir, "2001-01-02"),
        lambda: analysis.migrate_analysis(data_dir),
    ):
        with pytest.raises(AnalysisStorageError, match="analysis_schema_"):
            call()


def test_atomic_snapshots_current_projection_and_immutable_audit(data_dir):
    analysis.migrate_analysis(data_dir)
    first = waiting()
    first_run = str(uuid4())
    analysis.save_results(data_dir, [first], run_id=first_run)
    second = ready(checked_at="2001-01-02T11:00:00Z")
    second_run = str(uuid4())
    analysis.save_results(data_dir, [second], run_id=second_run)
    latest = analysis.read_latest(data_dir, "2001-01-02")
    assert latest == {**second, "check_id": 2, "run_id": second_run}
    older = ready(checked_at="2001-01-02T09:00:00Z")
    older["source_fingerprint"] = "b" * 64
    assert analysis.save_results(data_dir, [older], run_id=str(uuid4()))["current_updated"] == 0
    assert analysis.read_latest(data_dir, "2001-01-02") == latest
    assert len(rows(data_dir, "analysis_readiness_checks")) == 3
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection:
        analysis.validate_analysis(connection)
        for statement in (
            "DELETE FROM analysis_readiness_checks",
            "UPDATE analysis_readiness_checks SET core_ready=0",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="analysis_audit_immutable"):
                connection.execute(statement)


def test_duplicate_run_is_idempotent_and_conflicting_batch_is_atomic(data_dir):
    analysis.migrate_analysis(data_dir)
    run_id = str(uuid4())
    first = ready()
    analysis.save_results(data_dir, [first], run_id=run_id)
    assert analysis.save_results(data_dir, [first], run_id=run_id) == {
        "status": "saved",
        "inserted": 0,
        "current_updated": 0,
        "duplicates": 1,
        "result_count": 1,
    }
    second = ready("2001-01-03", "2001-01-03T10:00:00Z")
    changed = {**first, "source_fingerprint": "b" * 64}
    with pytest.raises(AnalysisStorageError, match="analysis_run_conflict"):
        analysis.save_results(data_dir, [second, changed], run_id=run_id)
    assert analysis.read_latest(data_dir, second["night_date"]) is None
    assert len(rows(data_dir, "analysis_readiness_checks")) == 1


def test_database_failure_after_first_result_rolls_back_batch(data_dir):
    analysis.migrate_analysis(data_dir)
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection, connection:
        connection.execute("""CREATE TRIGGER synthetic_failure
            BEFORE INSERT ON analysis_readiness_checks
            WHEN NEW.night_date='2001-01-03' BEGIN SELECT RAISE(ABORT,'PRIVATE FAILURE'); END""")
    with pytest.raises(AnalysisStorageError, match="^analysis_write_failed$"):
        analysis.save_results(
            data_dir, [ready(), ready("2001-01-03", "2001-01-03T10:00:00Z")], run_id=str(uuid4())
        )
    assert rows(data_dir, "analysis_readiness_checks") == []
    assert rows(data_dir, "analysis_readiness") == []


@pytest.mark.parametrize(
    "mutation", ["nan", "ready_missing", "hash", "extra", "huge", "date", "negative", "duration"]
)
def test_bad_payload_is_rejected_without_partial_writes_or_private_errors(data_dir, mutation):
    analysis.migrate_analysis(data_dir)
    value = ready("2001-01-03", "2001-01-03T10:00:00Z")
    if mutation == "nan":
        value["environment_summary"]["observed_windows"] = float("nan")
    elif mutation == "ready_missing":
        value["missing"] = waiting()["missing"]
    elif mutation == "hash":
        value["source_fingerprint"] = "PRIVATE HASH"
    elif mutation == "extra":
        value["secret"] = "PRIVATE TEXT"
    elif mutation == "huge":
        value["limitations"] = ["x" * 10000]
    elif mutation == "date":
        value["night_date"] = "2001-02-31"
    elif mutation == "negative":
        value = waiting()
        value["missing"][0]["observed"] = -1
    else:
        value["window"]["duration_seconds"] = 1
    with pytest.raises(AnalysisStorageError, match="^analysis_payload_invalid$"):
        analysis.save_results(data_dir, [ready(), value], run_id=str(uuid4()))
    assert rows(data_dir, "analysis_readiness_checks") == []


def test_expired_complete_and_unfinished_waiting_windows_remain_representable(data_dir):
    analysis.migrate_analysis(data_dir)
    expired = {**ready(), "data_state": "expired_unanalyzed"}
    analysis.save_results(data_dir, [expired], run_id=str(uuid4()))
    assert analysis.read_latest(data_dir, "2001-01-02")["core_ready"] is True
    unfinished = waiting()
    unfinished["checked_at"] = "2001-01-02T07:00:00Z"
    analysis.save_results(data_dir, [unfinished], run_id=str(uuid4()))
    with pytest.raises(AnalysisStorageError, match="analysis_payload_invalid"):
        analysis.save_results(
            data_dir, [{**ready(), "checked_at": "2001-01-02T07:00:00Z"}], run_id=str(uuid4())
        )


def test_read_bulk_uses_caller_snapshot_without_ending_it(data_dir):
    analysis.migrate_analysis(data_dir)
    analysis.save_results(data_dir, [waiting()], run_id=str(uuid4()))
    with closing(
        sqlite3.connect((data_dir / "sleepscape.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        initial = analysis.read_bulk(data_dir, ["2001-01-02"], connection=connection)
        analysis.save_results(
            data_dir, [ready(checked_at="2001-01-02T11:00:00Z")], run_id=str(uuid4())
        )
        assert analysis.read_bulk(data_dir, ["2001-01-02"], connection=connection) == initial
        assert connection.in_transaction
    assert analysis.read_latest(data_dir, "2001-01-02")["core_ready"] is True


@pytest.mark.parametrize(
    "corruption", ["hash", "payload", "current_missing", "current_older", "wrong_night"]
)
def test_corrupt_snapshots_and_projection_are_rejected(data_dir, corruption):
    analysis.migrate_analysis(data_dir)
    analysis.save_results(data_dir, [waiting()], run_id=str(uuid4()))
    analysis.save_results(data_dir, [ready(checked_at="2001-01-02T11:00:00Z")], run_id=str(uuid4()))
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection, connection:
        if corruption in {"hash", "payload"}:
            connection.execute("DROP TRIGGER analysis_readiness_checks_no_update")
            field = "payload_hash" if corruption == "hash" else "payload_json"
            connection.execute(
                f"UPDATE analysis_readiness_checks SET {field}=? WHERE check_id=2",
                ("PRIVATE INVALID",),
            )
            connection.execute(analysis._DDL["analysis_readiness_checks_no_update"])
        elif corruption == "current_missing":
            connection.execute("DELETE FROM analysis_readiness")
        elif corruption == "current_older":
            connection.execute("UPDATE analysis_readiness SET check_id=1")
        else:
            connection.execute("UPDATE analysis_readiness SET night_date='2001-01-03'")
    with pytest.raises(AnalysisStorageError):
        analysis.read_bulk(data_dir, ["2001-01-02", "2001-01-03"])
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as connection:
        with pytest.raises(AnalysisStorageError):
            analysis.validate_analysis(connection)


def test_capacity_blocks_new_writes_and_migration_but_not_reads(data_dir, monkeypatch):
    def blocked(*args, **kwargs):
        raise CapacityError("storage_critical")

    monkeypatch.setattr(analysis, "require_capacity", blocked)
    with pytest.raises(AnalysisStorageError, match="analysis_capacity_blocked"):
        analysis.migrate_analysis(data_dir)
    assert analysis.read_latest(data_dir, "2001-01-02") is None
    monkeypatch.undo()
    analysis.migrate_analysis(data_dir)
    analysis.save_results(data_dir, [ready()], run_id=str(uuid4()))
    monkeypatch.setattr(analysis, "require_capacity", blocked)
    with pytest.raises(AnalysisStorageError, match="analysis_capacity_blocked"):
        analysis.save_results(data_dir, [ready()], run_id=str(uuid4()))
    assert analysis.read_latest(data_dir, "2001-01-02")["core_ready"]


def test_optional_manual_and_unsupported_inputs_preserve_exact_observations(data_dir):
    analysis.migrate_analysis(data_dir)
    value = ready()
    issue = waiting()["missing"][0]
    value["optional_missing"] = [
        {**issue, "code": "optional_unavailable", "source": "manual", "metric": "lifestyle"},
        {
            **issue,
            "code": "optional_unavailable",
            "metric": "sleep.stage_timeline",
            "observed": "unsupported",
        },
    ]
    analysis.save_results(data_dir, [value], run_id=str(uuid4()))
    assert (
        analysis.read_latest(data_dir, "2001-01-02")["optional_missing"]
        == value["optional_missing"]
    )
    invalid = waiting()
    invalid["missing"][0]["source"] = "manual"
    with pytest.raises(AnalysisStorageError, match="analysis_payload_invalid"):
        analysis.save_results(data_dir, [invalid], run_id=str(uuid4()))


def test_read_bounds_and_symlinks_are_safe(data_dir, tmp_path):
    with pytest.raises(AnalysisStorageError, match="analysis_date_range_invalid"):
        analysis.read_bulk(data_dir, ["2001-01-02"] * 93)
    with pytest.raises(AnalysisStorageError, match="analysis_payload_invalid"):
        analysis.read_latest(data_dir, "2001-02-31")
    link = tmp_path / "private-link"
    link.symlink_to(data_dir, target_is_directory=True)
    with pytest.raises(AnalysisStorageError, match="analysis_source_unavailable"):
        analysis.read_latest(link, "2001-01-02")


def test_large_exact_evidence_is_saved_below_four_mib_and_rejected_above(data_dir, monkeypatch):
    analysis.migrate_analysis(data_dir)
    value = ready()
    value["environment_summary"]["synthetic_padding"] = []
    base, _ = analysis._validate_payload(value)
    chunks = (analysis.MAX_RESULT_BYTES - len(base.encode("utf-8"))) // 1027
    value["environment_summary"]["synthetic_padding"] = ["x" * 1024] * chunks
    encoded, _ = analysis._validate_payload(value)
    byte_count = len(encoded.encode("utf-8"))
    assert analysis.MAX_RESULT_BYTES - 2048 < byte_count <= analysis.MAX_RESULT_BYTES
    admission = []
    original = analysis.require_capacity

    def capture(directory, operation, required_bytes=0):
        admission.append((operation, required_bytes))
        return original(directory, operation, required_bytes)

    monkeypatch.setattr(analysis, "require_capacity", capture)
    analysis.save_results(data_dir, [value], run_id=str(uuid4()))
    assert admission == [("write", byte_count * 2)]
    assert (
        analysis.read_latest(data_dir, "2001-01-02")["environment_summary"]
        == value["environment_summary"]
    )
    value["environment_summary"]["synthetic_padding"].extend(["x" * 1024] * 2)
    assert len(json.dumps(value).encode("utf-8")) > analysis.MAX_RESULT_BYTES
    with pytest.raises(AnalysisStorageError, match="analysis_payload_invalid"):
        analysis.save_results(data_dir, [value], run_id=str(uuid4()))
    assert len(rows(data_dir, "analysis_readiness_checks")) == 1


def test_full_day_fragmented_missing_evidence_is_not_truncated(data_dir):
    analysis.migrate_analysis(data_dir)
    value = waiting()
    issue = value["missing"][0]
    value["missing"] = [{**issue, "metric": f"synthetic.interval_{i}"} for i in range(2880)]
    value["environment_summary"]["channels"] = {"temperature": None}
    analysis.save_results(data_dir, [value], run_id=str(uuid4()))
    assert analysis.read_latest(data_dir, "2001-01-02")["missing"] == value["missing"]
    with pytest.raises(AnalysisStorageError, match="analysis_batch_invalid"):
        analysis.save_results(data_dir, [ready()] * 32, run_id=str(uuid4()))
