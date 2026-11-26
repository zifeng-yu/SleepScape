"""Synthetic reports: durable attempts, one publication and recoverable private evidence."""

import copy
import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import requests
from test_sleep_features import example as read_report_example

from sleepscape.config import Settings
from sleepscape.services import backup_bundle
from sleepscape.services.backup import BackupError, check_restore, create_backup
from sleepscape.storage import analysis
from sleepscape.storage import sleep_analysis as store
from sleepscape.storage.backup_manifest import MaintenanceError, write_json
from sleepscape.storage.repository import Repository
from sleepscape.storage.sleep_analysis import SleepAnalysisStorageError
from sleepscape.storage.usage import CapacityError

CREATED = "2001-01-02T02:00:00Z"
STARTED = "2001-01-02T02:01:00Z"
FINISHED = "2001-01-02T02:02:00Z"
DAY = "2001-01-02"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(key)

    def forbidden(*args, **kwargs):
        pytest.fail("Report storage tests cannot call a network")

    monkeypatch.setattr(requests.Session, "send", forbidden)


@pytest.fixture
def data_dir(tmp_path):
    value = tmp_path / "data"
    with Repository(value):
        pass
    return value


@pytest.fixture
def example():
    return read_report_example()


def settings(data_dir):
    return Settings(_env_file=None, data_dir=data_dir)


def put(data_dir, example, **kwargs):
    metadata = {
        "source_fingerprint": "a" * 64,
        "prompt_hash": "b" * 64,
        "schema_hash": "e" * 64,
        "prompt_version": "sleep-observation-1",
        "model_ref": "sleep_analyst",
        "config_fingerprint": "c" * 64,
        "time_mapping_version": "boot-fixed-offset-1",
        "now": CREATED,
    }
    metadata.update(kwargs)
    return store.save_input(data_dir, example["input"], **metadata)


def make_attempt(data_dir, example, *, source_fingerprint="a" * 64):
    source = put(data_dir, example, source_fingerprint=source_fingerprint)
    return store.begin_attempt(data_dir, source["input_id"], request_id=uuid4().hex, now=STARTED)


def publish(data_dir, example):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    return store.publish_report(data_dir, attempt["attempt_id"], example["output"], now=FINISHED)


def test_readonly_missing_database_and_schema_never_create(tmp_path, data_dir):
    missing = tmp_path / "missing"
    assert store.read_report(missing, DAY) is None
    assert store.read_input(missing, uuid4().hex) is None
    assert store.read_attempts(missing, DAY) == []
    assert store.read_attempted_nights(missing, DAY) == []
    assert not missing.exists()
    assert store.read_report(data_dir, DAY) is None
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c:
        assert not store.report_available(c)


def test_migration_is_explicit_idempotent_and_keeps_readiness_schema(data_dir):
    analysis.migrate_analysis(data_dir)
    assert store.migrate_reports(data_dir) == {"status": "initialized", "schema_version": 1}
    assert store.migrate_reports(data_dir)["status"] == "already_initialized"
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c:
        assert store.report_available(c)
        assert c.execute("SELECT version FROM analysis_schema").fetchall() == [(1,)]
        store.validate_reports(c)
    assert (data_dir / "sleepscape.sqlite3").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "mutation", ["missing_table", "missing_trigger", "wrong_version", "altered_index"]
)
def test_partial_or_tampered_schema_rejected(data_dir, mutation):
    store.migrate_reports(data_dir)
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c, c:
        if mutation == "missing_table":
            c.execute("DROP TABLE sleep_analysis_reports")
        elif mutation == "missing_trigger":
            c.execute("DROP TRIGGER analysis_inputs_no_update")
        elif mutation == "wrong_version":
            c.execute("UPDATE sleep_analysis_schema SET version=99")
        else:
            c.execute("DROP INDEX analysis_attempts_started")
            c.execute("CREATE INDEX analysis_attempts_started ON analysis_attempts(state)")
    with pytest.raises(SleepAnalysisStorageError, match="sleep_analysis_schema_"):
        store.migrate_reports(data_dir)
    with pytest.raises(BackupError, match="backup_sleep_analysis_schema_invalid"):
        create_backup(settings(data_dir))


def test_input_immutable_and_semantic_duplicates_keep_original_cutoff(data_dir, example):
    store.migrate_reports(data_dir)
    first = put(data_dir, example)
    later = copy.deepcopy(example)
    later["input"]["data_cutoff_at"] = "2001-01-02T01:10:00Z"
    assert put(data_dir, later, now=STARTED) == first
    changed = put(data_dir, example, source_fingerprint="d" * 64)
    assert changed["input_id"] != first["input_id"]
    assert changed["input_hash"] == first["input_hash"]
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c, c:
        for sql in ("UPDATE analysis_inputs SET model_ref='other'", "DELETE FROM analysis_inputs"):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)
    assert store.read_input(data_dir, first["input_id"]) == first


def test_input_metadata_reuse_cannot_relabel_a_snapshot(data_dir, example):
    store.migrate_reports(data_dir)
    put(data_dir, example)
    with pytest.raises(SleepAnalysisStorageError, match="sleep_analysis_input_conflict"):
        put(data_dir, example, model_ref="unrelated_model")


def test_schema_hash_is_retained_and_changes_snapshot_identity(data_dir, example):
    store.migrate_reports(data_dir)
    first = put(data_dir, example)
    changed = put(data_dir, example, schema_hash="f" * 64)
    assert changed["input_id"] != first["input_id"]
    assert changed["input_hash"] == first["input_hash"]
    assert changed["prompt_hash"] == first["prompt_hash"]
    assert changed["schema_hash"] == "f" * 64
    assert store.read_input(data_dir, first["input_id"])["schema_hash"] == "e" * 64
    assert put(data_dir, example, schema_hash="f" * 64) == changed
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c:
        store.validate_reports(c)


@pytest.mark.parametrize("value", [None, "", "g" * 64, "f" * 63, "F" * 64])
def test_invalid_schema_hash_is_rejected(data_dir, example, value):
    store.migrate_reports(data_dir)
    with pytest.raises(SleepAnalysisStorageError):
        put(data_dir, example, schema_hash=value)


@pytest.mark.parametrize(
    "mutation", ["wrong_hash", "unexpected_private_field", "future_cutoff", "wrong_quality"]
)
def test_invalid_input_rejected_before_write(data_dir, example, mutation):
    store.migrate_reports(data_dir)
    value = copy.deepcopy(example)
    if mutation == "wrong_hash":
        value["input"]["input_hash"] = "0" * 64
    elif mutation == "unexpected_private_field":
        value["input"]["private_device_id"] = "SYNTHETIC-PRIVATE"
    elif mutation == "future_cutoff":
        value["input"]["data_cutoff_at"] = "2001-01-02T03:00:00Z"
    else:
        value["input"]["completeness"]["quality_state"] = "insufficient"
    with pytest.raises(SleepAnalysisStorageError):
        put(data_dir, value)
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c:
        assert c.execute("SELECT COUNT(*) FROM analysis_inputs").fetchone()[0] == 0


def test_attempt_durable_request_identity_failure_and_idempotence(data_dir, example):
    store.migrate_reports(data_dir)
    source = put(data_dir, example)
    request = uuid4().hex
    first = store.begin_attempt(data_dir, source["input_id"], request_id=request, now=STARTED)
    assert (
        store.begin_attempt(data_dir, source["input_id"], request_id=request, now=FINISHED) == first
    )
    assert store.read_attempts(data_dir, DAY) == [first]
    failed = store.finish_attempt(
        data_dir,
        first["attempt_id"],
        state="failed",
        error_code="rate_limited",
        retry_after=120,
        now=FINISHED,
    )
    assert failed["input_hash"] == example["input"]["input_hash"]
    assert failed["state"] == "failed" and failed["retry_after"] == 120
    assert (
        store.finish_attempt(
            data_dir,
            first["attempt_id"],
            state="failed",
            error_code="rate_limited",
            retry_after=120,
            now="2001-01-02T03:00:00Z",
        )
        == failed
    )
    with pytest.raises(SleepAnalysisStorageError, match="sleep_analysis_attempt_conflict"):
        store.finish_attempt(
            data_dir,
            first["attempt_id"],
            state="abandoned",
            error_code="analysis_interrupted",
            now=FINISHED,
        )
    other = put(data_dir, example, source_fingerprint="d" * 64)
    with pytest.raises(SleepAnalysisStorageError, match="sleep_analysis_request_conflict"):
        store.begin_attempt(data_dir, other["input_id"], request_id=request, now=FINISHED)


@pytest.mark.parametrize(
    "state,error",
    [
        ("discarded_input_changed", "input_changed"),
        ("discarded_already_published", "already_published"),
        ("abandoned", "analysis_interrupted"),
    ],
)
def test_nonpublishing_terminal_attempts_retained(data_dir, example, state, error):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    result = store.finish_attempt(
        data_dir, attempt["attempt_id"], state=state, error_code=error, now=FINISHED
    )
    assert result["state"] == state
    assert store.read_report(data_dir, DAY) is None
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c:
        store.validate_reports(c)


def test_no_raw_failure_or_arbitrary_state_persisted(data_dir, example):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    with pytest.raises(SleepAnalysisStorageError):
        store.finish_attempt(
            data_dir,
            attempt["attempt_id"],
            state="failed",
            error_code="SYNTHETIC PRIVATE RESPONSE",
            now=FINISHED,
        )
    with pytest.raises(SleepAnalysisStorageError):
        store.finish_attempt(data_dir, attempt["attempt_id"], state="complete", now=FINISHED)
    assert store.read_attempts(data_dir, DAY)[0]["state"] == "running"


def test_report_is_unique_immutable_and_same_attempt_replay_is_idempotent(data_dir, example):
    result = publish(data_dir, example)
    assert result["output"] == example["output"]
    assert result["input"]["payload"] == example["input"]
    assert result["attempt"]["state"] == "complete"
    assert (
        store.publish_report(
            data_dir, result["attempt_id"], example["output"], now="2001-01-02T03:00:00Z"
        )
        == result
    )
    with pytest.raises(SleepAnalysisStorageError, match="already_published"):
        store.begin_attempt(
            data_dir, result["input"]["input_id"], request_id=uuid4().hex, now=FINISHED
        )
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c, c:
        for sql in (
            "DELETE FROM sleep_analysis_reports",
            "UPDATE sleep_analysis_reports SET generated_at='2001-01-02T03:00:00Z'",
            "DELETE FROM analysis_attempts",
            "UPDATE analysis_attempts SET state='running'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)
        store.validate_reports(c)


def test_concurrent_publish_cannot_overwrite_the_winning_report(data_dir, example):
    store.migrate_reports(data_dir)
    first, second = make_attempt(data_dir, example), make_attempt(data_dir, example)

    def attempt(value):
        try:
            return store.publish_report(
                data_dir, value["attempt_id"], example["output"], now=FINISHED
            )["attempt_id"]
        except SleepAnalysisStorageError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(attempt, [first, second]))
    assert values.count("sleep_analysis_already_published") == 1
    winner = store.read_report(data_dir, DAY)
    assert winner["attempt_id"] in values
    assert len(store.read_attempts(data_dir, DAY)) == 2


@pytest.mark.parametrize(
    "mutation", ["reference", "hash", "quality", "restriction", "extra", "refusal"]
)
def test_invalid_output_leaves_attempt_running_and_no_report(data_dir, example, mutation):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    value = copy.deepcopy(example["output"])
    if mutation == "reference":
        value["summary"]["evidence_ids"] = ["F999"]
    elif mutation == "hash":
        value["input_hash"] = "0" * 64
    elif mutation == "quality":
        value["input_quality_state"] = "complete"
    elif mutation == "restriction":
        value["limitations"] = []
    elif mutation == "extra":
        value["private_chain_of_thought"] = "SYNTHETIC PRIVATE"
    else:
        value["status"] = "insufficient_input"
    with pytest.raises(SleepAnalysisStorageError):
        store.publish_report(data_dir, attempt["attempt_id"], value, now=FINISHED)
    assert store.read_report(data_dir, DAY) is None
    assert store.read_attempts(data_dir, DAY)[0]["state"] == "running"


def test_all_shanghai_day_attempts_count_even_outside_current_lookback(data_dir, example):
    store.migrate_reports(data_dir)
    source = put(data_dir, example)
    for when in ("2001-01-02T15:59:59Z", "2001-01-02T16:00:00Z", "2001-01-03T15:59:59Z"):
        store.begin_attempt(data_dir, source["input_id"], request_id=uuid4().hex, now=when)
    assert store.read_attempted_nights(data_dir, "2001-01-01") == []
    assert store.read_attempted_nights(data_dir, "2001-01-02") == [DAY]
    assert store.read_attempted_nights(data_dir, "2001-01-03") == [DAY]
    assert store.read_attempted_nights(data_dir, "2001-01-04") == []
    assert len(store.read_attempts(data_dir, DAY, input_hash=source["input_hash"])) == 3
    assert store.read_attempts(data_dir, DAY, input_hash="f" * 64) == []
    with pytest.raises(SleepAnalysisStorageError):
        store.read_attempts(data_dir, DAY, input_hash="")


def test_capacity_prevents_any_new_schema_or_input_or_attempt(data_dir, example, monkeypatch):
    def fail(*args, **kwargs):
        raise CapacityError("storage_capacity_critical")

    with monkeypatch.context() as context:
        context.setattr(store, "require_capacity", fail)
        with pytest.raises(SleepAnalysisStorageError, match="capacity_blocked"):
            store.migrate_reports(data_dir)
    store.migrate_reports(data_dir)
    source = put(data_dir, example)
    monkeypatch.setattr(store, "require_capacity", fail)
    with pytest.raises(SleepAnalysisStorageError, match="capacity_blocked"):
        put(data_dir, example, source_fingerprint="e" * 64)
    with pytest.raises(SleepAnalysisStorageError, match="capacity_blocked"):
        store.begin_attempt(data_dir, source["input_id"], request_id=uuid4().hex, now=STARTED)
    assert store.read_attempts(data_dir, DAY) == []


@pytest.mark.parametrize("managed", [False, True])
def test_new_report_backup_restores_inputs_attempts_and_report(data_dir, example, managed):
    expected = publish(data_dir, example)
    config = settings(data_dir)
    if managed:
        bid = uuid4().hex
        backup_bundle.create_bundle(config, bid, datetime(2001, 1, 3, tzinfo=UTC))
        path = backup_bundle.bundle_directory(data_dir, bid)
        checked = backup_bundle.validate_bundle(path, restore=True)
    else:
        saved = create_backup(config)
        checked = check_restore(config, Path(saved["backup_path"]))
        path = data_dir / saved["backup_path"]
    assert checked["counts"]["analysis_inputs"] == 1
    assert checked["counts"]["analysis_attempts"] == 1
    assert checked["counts"]["sleep_analysis_reports"] == 1
    database = path / "sleepscape.sqlite3" if managed else path
    with closing(sqlite3.connect(database)) as c:
        store.validate_reports(c)
        assert store.read_report(data_dir, DAY, connection=c) == expected


def test_old_backup_without_report_schema_still_restores(data_dir, example):
    config = settings(data_dir)
    saved = create_backup(config)
    publish(data_dir, example)
    checked = check_restore(config, Path(saved["backup_path"]))
    assert "sleep_analysis_reports" not in checked["counts"]
    assert store.read_report(data_dir, DAY) is not None


def test_caller_snapshot_is_preserved_and_reads_do_not_write(data_dir, example):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c:
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        assert store.read_report(data_dir, DAY, connection=c) is None
        store.publish_report(data_dir, attempt["attempt_id"], example["output"], now=FINISHED)
        assert store.read_report(data_dir, DAY, connection=c) is None
        assert store.read_attempts(data_dir, DAY, connection=c)[0]["state"] == "running"
        assert c.total_changes == 0
    assert store.read_report(data_dir, DAY)["attempt"]["state"] == "complete"


def test_publication_failure_rolls_back_attempt_transition(data_dir, example):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c, c:
        c.execute(
            "CREATE TRIGGER synthetic_failure BEFORE INSERT ON sleep_analysis_reports "
            "BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END"
        )
    with pytest.raises(SleepAnalysisStorageError, match="sleep_analysis_write_failed"):
        store.publish_report(data_dir, attempt["attempt_id"], example["output"], now=FINISHED)
    assert store.read_report(data_dir, DAY) is None
    assert store.read_attempts(data_dir, DAY)[0]["state"] == "running"


def test_publication_guard_uses_write_transaction_and_committed_source_snapshot(data_dir, example):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    with Repository(data_dir) as repository:
        repository.start_run({"purpose": "synthetic_source_changed_during_model_call"})
    observed = []

    def guard(connection):
        assert connection.in_transaction
        observed.append(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        return observed[-1] == 0

    with pytest.raises(SleepAnalysisStorageError, match="sleep_analysis_source_changed"):
        store.publish_report(
            data_dir, attempt["attempt_id"], example["output"], guard=guard, now=FINISHED
        )
    assert observed == [1]
    assert store.read_report(data_dir, DAY) is None
    assert store.read_attempts(data_dir, DAY)[0]["state"] == "running"
    store.finish_attempt(
        data_dir,
        attempt["attempt_id"],
        state="discarded_input_changed",
        error_code="input_changed",
        now=FINISHED,
    )
    assert store.read_attempts(data_dir, DAY)[0]["state"] == "discarded_input_changed"


def test_true_publication_guard_prevents_an_interleaved_writer(data_dir, example):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)

    def guard(connection):
        assert connection.in_transaction
        with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3", timeout=0)) as competing:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("BEGIN IMMEDIATE")
        return True

    result = store.publish_report(
        data_dir, attempt["attempt_id"], example["output"], guard=guard, now=FINISHED
    )
    assert result["attempt"]["state"] == "complete"


def test_complete_attempt_without_report_is_not_read_as_success(data_dir, example):
    store.migrate_reports(data_dir)
    make_attempt(data_dir, example)
    with closing(sqlite3.connect(data_dir / "sleepscape.sqlite3")) as c, c:
        c.execute(
            "UPDATE analysis_attempts SET state='complete',finished_at=?",
            ("2001-01-02T02:02:00.000000Z",),
        )
    with pytest.raises(SleepAnalysisStorageError, match="sleep_analysis_reference_invalid"):
        store.read_attempts(data_dir, DAY)
    with pytest.raises(BackupError, match="backup_sleep_analysis_invalid"):
        create_backup(settings(data_dir))


@pytest.mark.parametrize("normalization", [None, "json_string_envelope", "single_object_array"])
def test_model_metadata_persists_beyond_runtime_and_summary_is_content_free(
    data_dir, example, normalization
):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    metadata = {
        "model": "synthetic-model-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        "duration_ms": 500,
        "finish_reason": "stop",
    }
    if normalization is not None:
        metadata["transport_normalization"] = normalization
    result = store.publish_report(
        data_dir, attempt["attempt_id"], example["output"], model_metadata=metadata, now=FINISHED
    )
    assert result["model_metadata"] == metadata
    assert not (data_dir / "model-runtime.json").exists()
    assert store.read_report_summaries(data_dir, [DAY, "2001-01-03", DAY]) == {
        DAY: {"state": "complete", "generated_at": result["generated_at"]}
    }
    with pytest.raises(SleepAnalysisStorageError, match="date_range_invalid"):
        store.read_report_summaries(data_dir, [DAY] * 93)
    checked = check_restore(
        settings(data_dir), Path(create_backup(settings(data_dir))["backup_path"])
    )
    assert checked["status"] == "verified"
    assert (
        store.publish_report(data_dir, attempt["attempt_id"], example["output"], now=FINISHED)
        == result
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "http://private.example/key?secret=synthetic"),
        ("model", "https://private.example/model"),
        ("usage", {"secret": "synthetic"}),
        ("usage", {"prompt_tokens": -1}),
        ("usage", {"total_tokens": True}),
        ("duration_ms", -1),
        ("finish_reason", "length"),
        ("private", "synthetic secret"),
        ("transport_normalization", "none"),
        ("transport_normalization", "synthetic secret"),
        ("transport_normalization", None),
        ("transport_normalization", True),
        ("transport_normalization", []),
    ],
)
def test_model_metadata_rejects_unknown_private_fields(data_dir, example, field, value):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    metadata = {
        "model": "synthetic-model-1",
        "usage": {},
        "duration_ms": 500,
        "finish_reason": "stop",
    }
    metadata[field] = value
    with pytest.raises(SleepAnalysisStorageError, match="model_metadata_invalid"):
        store.publish_report(
            data_dir,
            attempt["attempt_id"],
            example["output"],
            model_metadata=metadata,
            now=FINISHED,
        )
    assert store.read_report(data_dir, DAY) is None


@pytest.mark.parametrize("normalization", ["json_string_envelope", "single_object_array"])
@pytest.mark.parametrize("tamper", ["hash_mismatch", "unknown_normalization_rehashed"])
def test_normalization_metadata_tampering_is_rejected_by_restore(
    data_dir, example, tamper, normalization
):
    store.migrate_reports(data_dir)
    attempt = make_attempt(data_dir, example)
    metadata = {
        "model": "synthetic-model-1",
        "usage": {},
        "duration_ms": 500,
        "finish_reason": "stop",
        "transport_normalization": normalization,
    }
    store.publish_report(
        data_dir, attempt["attempt_id"], example["output"], model_metadata=metadata, now=FINISHED
    )
    config = settings(data_dir)
    path = data_dir / create_backup(config)["backup_path"]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER sleep_analysis_reports_no_update")
        if tamper == "hash_mismatch":
            changed = {
                key: value for key, value in metadata.items() if key != "transport_normalization"
            }
            connection.execute(
                "UPDATE sleep_analysis_reports SET model_json=?", (store._json(changed),)
            )
        else:
            changed = {**metadata, "transport_normalization": "SYNTHETIC_PRIVATE_UNKNOWN"}
            raw = store._json(changed)
            connection.execute(
                "UPDATE sleep_analysis_reports SET model_json=?,model_hash=?",
                (raw, hashlib.sha256(raw.encode()).hexdigest()),
            )
        connection.execute(store._DDL["sleep_analysis_reports_no_update"])
    with pytest.raises(BackupError, match="backup_sleep_analysis_invalid"):
        check_restore(config, path)
    assert store.read_report(data_dir, DAY)["model_metadata"] == metadata


def test_existing_readiness_audits_preserved_in_report_backup(data_dir, example):
    from test_analysis_storage import ready

    analysis.migrate_analysis(data_dir)
    analysis.save_results(data_dir, [ready()], run_id=uuid4().hex)
    path = data_dir / "sleepscape.sqlite3"
    with closing(sqlite3.connect(path)) as c:
        original = c.execute("SELECT * FROM analysis_readiness_checks").fetchall()
    publish(data_dir, example)
    saved = create_backup(settings(data_dir))
    checked = check_restore(settings(data_dir), Path(saved["backup_path"]))
    assert checked["counts"]["analysis_readiness_checks"] == 1
    assert checked["counts"]["sleep_analysis_reports"] == 1
    with closing(sqlite3.connect(data_dir / saved["backup_path"])) as c:
        assert c.execute("SELECT * FROM analysis_readiness_checks").fetchall() == original
        analysis.validate_analysis(c)
        store.validate_reports(c)


def corrupt_report(path):
    with closing(sqlite3.connect(path)) as c, c:
        c.execute("DROP TRIGGER sleep_analysis_reports_no_update")
        raw = c.execute("SELECT payload_json FROM sleep_analysis_reports").fetchone()[0]
        value = json.loads(raw)
        value["summary"]["evidence_ids"] = ["F999"]
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        c.execute(
            "UPDATE sleep_analysis_reports SET payload_json=?,payload_hash=?",
            (encoded, hashlib.sha256(encoded.encode()).hexdigest()),
        )
        c.execute(store._DDL["sleep_analysis_reports_no_update"])


@pytest.mark.parametrize("managed", [False, True])
def test_recomputed_file_and_payload_hashes_cannot_restore_false_evidence(
    data_dir, example, managed
):
    publish(data_dir, example)
    config = settings(data_dir)
    if managed:
        bid = uuid4().hex
        backup_bundle.create_bundle(config, bid, datetime(2001, 1, 3, tzinfo=UTC))
        path = backup_bundle.bundle_directory(data_dir, bid)
        manifest = backup_bundle.validate_bundle(path)
        corrupt_report(path / backup_bundle.DATABASE)
        manifest["files"][backup_bundle.DATABASE] = backup_bundle.file_digest(
            path / backup_bundle.DATABASE
        )
        write_json(path / backup_bundle.MANIFEST, manifest)
        with pytest.raises(MaintenanceError, match="maintenance_bundle_invalid"):
            backup_bundle.validate_bundle(path, restore=True)
    else:
        saved = create_backup(config)
        path = data_dir / saved["backup_path"]
        corrupt_report(path)
        with pytest.raises(BackupError, match="backup_sleep_analysis_invalid"):
            check_restore(config, path)
    assert store.read_report(data_dir, DAY)["output"] == example["output"]
