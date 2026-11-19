"""Synthetic metadata only; never opens the user's database or credentials."""

import json
import multiprocessing
import os
from datetime import UTC, datetime, timedelta

import pytest

from sleepscape.config import Settings
from sleepscape.models.base import ModelError, ModelResult
from sleepscape.services import model_health as health
from sleepscape.storage import model_runtime as runtime

NOW = datetime(2026, 11, 17, 1, tzinfo=UTC)
FINGERPRINT = "a" * 64


def private_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text)
    path.chmod(0o600)


@pytest.fixture
def configured(tmp_path):
    key = tmp_path / "credentials" / "synthetic-key"
    private_file(key, "synthetic-key-never-transmitted")
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        llm_enabled=True,
        llm_base_url="https://example.invalid/v1",
        llm_endpoint=None,
        llm_key_file=key,
    )


def successful_reply(model, request):
    nonce = request.output_schema["properties"]["nonce"]["const"]
    return ModelResult(
        json.dumps({"ok": True, "nonce": nonce, "items": [1, 2], "note": "synthetic"}),
        model.model,
        "stop",
        {"prompt_tokens": 31, "completion_tokens": 18, "total_tokens": 49},
    )


def hold_runtime_lock(data_dir, connection):
    with runtime.session(data_dir):
        connection.send("locked")
        connection.recv()
    connection.close()


def test_status_missing_configuration_is_read_only(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "absent",
        llm_base_url=None,
        llm_endpoint=None,
        llm_key_file=tmp_path / "absent-key",
    )
    monkeypatch.setattr(health, "call_model", lambda *_: pytest.fail("No outbound call"))
    status = health.model_status(settings)
    assert status["connection_state"] == "not_checked"
    assert not status["configured"]
    check = health.run_model_check(settings)
    assert not check["attempted"]
    assert check["status"] == "failed"
    assert not settings.data_dir.exists()


def test_status_does_not_read_credential_or_create_state(configured, monkeypatch):
    from sleepscape.models import settings as model_settings

    monkeypatch.setattr(
        model_settings, "read_api_key", lambda *_: pytest.fail("GET cannot read the key")
    )
    monkeypatch.setattr(health, "call_model", lambda *_: pytest.fail("GET cannot call model"))
    status = health.model_status(configured)
    assert status["configured"]
    assert status["budget"] == {"limit": 30, "used": 0, "remaining": 30}
    assert not configured.data_dir.exists()


def test_verified_check_persists_only_safe_metadata(configured, monkeypatch):
    monkeypatch.setattr(health, "call_model", successful_reply)
    result = health.run_model_check(configured)
    assert result["status"] == "verified"
    status = result["model"]
    assert status["connection_state"] == "verified"
    assert status["last_check_status"] == "verified"
    assert status["budget"] == {"limit": 30, "used": 1, "remaining": 29}
    assert status["usage"]["total_tokens"] == 49
    path = configured.data_dir / runtime.STATE_FILE
    raw = path.read_text()
    assert "synthetic-key-never-transmitted" not in raw
    assert "example.invalid" not in raw
    assert "nonce" not in raw
    assert "messages" not in raw
    assert "text" not in raw
    assert path.stat().st_mode & 0o777 == 0o600
    assert configured.data_dir.stat().st_mode & 0o777 == 0o700
    assert not list(configured.data_dir.glob("*.sqlite3"))


@pytest.mark.parametrize("bad", ["wrong_nonce", "bool_integer", "extra", "invalid_json"])
def test_check_requires_schema_and_semantic_values(configured, monkeypatch, bad):
    def reply(model, request):
        value = json.loads(successful_reply(model, request).text)
        if bad == "wrong_nonce":
            value["nonce"] = "wrong"
        elif bad == "bool_integer":
            value["items"] = [True, 2]
        elif bad == "extra":
            value["extra"] = "not permitted"
        return ModelResult("{" if bad == "invalid_json" else json.dumps(value), model.model, "stop")

    monkeypatch.setattr(health, "call_model", reply)
    result = health.run_model_check(configured)
    assert result["status"] == "failed"
    assert result["code"] == "model_check_failed"
    assert result["model"]["budget"]["used"] == 1


def test_thirty_physical_attempts_survive_reloads_and_failures(configured, monkeypatch):
    calls = []

    def failure(*_):
        calls.append(1)
        raise ModelError("network_error", retryable=True)

    monkeypatch.setattr(health, "call_model", failure)
    for used in range(1, 31):
        result = health.run_model_check(configured.model_copy())
        assert result["code"] == "network_error"
        assert result["model"]["budget"]["used"] == used
    result = health.run_model_check(configured)
    assert result["code"] == "budget_exhausted"
    assert result["attempted"] is False
    assert len(calls) == 30  # Zero retries, including retryable transport errors.


def test_configuration_and_key_rotation_invalidate_verified(configured, monkeypatch):
    monkeypatch.setattr(health, "call_model", successful_reply)
    assert health.run_model_check(configured)["status"] == "verified"
    replacement = configured.model_copy(update={"llm_base_url": "https://other.invalid/v1"})
    assert health.model_status(replacement)["connection_state"] == "configuration_changed"
    configured.llm_key_file.write_text("new-synthetic-key-not-transmitted")
    assert health.model_status(configured)["connection_state"] == "configuration_changed"


def test_configuration_change_during_call_cannot_verify(configured, monkeypatch):
    def reply(model, request):
        configured.llm_key_file.write_text("rotated-while-synthetic-call-running")
        return successful_reply(model, request)

    monkeypatch.setattr(health, "call_model", reply)
    result = health.run_model_check(configured)
    assert result["code"] == "model_configuration_invalid"
    assert result["model"]["connection_state"] == "configuration_changed"


def test_existing_lock_blocks_without_model_call(configured, monkeypatch):
    monkeypatch.setattr(health, "call_model", lambda *_: pytest.fail("Lock prevents call"))
    with runtime.session(configured.data_dir):
        result = health.run_model_check(configured)
    assert result["code"] == "model_busy"
    assert not result["attempted"]


def test_separate_process_lock_blocks_duplicate_check(configured, monkeypatch):
    monkeypatch.setattr(health, "call_model", lambda *_: pytest.fail("No concurrent call"))
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=hold_runtime_lock, args=(configured.data_dir, child))
    worker.start()
    child.close()
    try:
        assert parent.poll(5)
        assert parent.recv() == "locked"
        result = health.run_model_check(configured)
        assert result["code"] == "model_busy"
        assert not result["attempted"]
    finally:
        parent.send("release")
        parent.close()
        worker.join(5)
        if worker.is_alive():
            worker.kill()
            worker.join(1)
        worker.close()


@pytest.mark.parametrize(
    "corrupt",
    [
        "{",
        "{}",
        '{"schema_version":1,"schema_version":1,"attempts":[]}',
        "x" * (runtime.MAX_STATE_BYTES + 1),
    ],
)
def test_corrupt_state_fails_closed_without_overwrite(configured, monkeypatch, corrupt):
    path = configured.data_dir / runtime.STATE_FILE
    private_file(path, corrupt)
    monkeypatch.setattr(health, "call_model", lambda *_: pytest.fail("Corruption prevents call"))
    before = path.read_bytes()
    result = health.run_model_check(configured)
    assert result["code"] == "model_state_unavailable"
    assert not result["attempted"]
    assert result["model"]["budget"]["remaining"] is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["state", "lock", "directory"])
def test_symlinks_rejected(tmp_path, target):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    data = tmp_path / "data"
    if target == "directory":
        data.symlink_to(actual, target_is_directory=True)
    else:
        data.mkdir(mode=0o700)
        destination = actual / "target"
        private_file(destination, '{"schema_version":1,"attempts":[]}')
        (data / (runtime.STATE_FILE if target == "state" else runtime.LOCK_FILE)).symlink_to(
            destination
        )
    with pytest.raises(ModelError, match="model_state_unavailable"), runtime.session(data):
        pytest.fail("Must reject symlink")


@pytest.mark.parametrize("target", ["file", "directory"])
def test_open_permissions_not_repaired_by_get(tmp_path, target):
    path = tmp_path / "data" / runtime.STATE_FILE
    private_file(path, '{"schema_version":1,"attempts":[]}')
    selected = path if target == "file" else path.parent
    selected.chmod(0o644 if target == "file" else 0o755)
    previous = selected.stat().st_mode
    with pytest.raises(ModelError, match="model_state_unavailable"):
        runtime.read_state(path.parent)
    assert selected.stat().st_mode == previous


def test_fifo_state_rejected_without_blocking(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    os.mkfifo(data / runtime.STATE_FILE, mode=0o600)
    with pytest.raises(ModelError, match="model_state_unavailable"):
        runtime.read_state(data)


def test_reservation_survives_interrupted_process_and_rollover(tmp_path):
    data = tmp_path / "data"
    before_midnight = datetime(2026, 11, 17, 15, 59, tzinfo=UTC)
    with runtime.session(data) as ledger:
        ledger.reserve(FINGERPRINT, before_midnight)
    # A new session observes the old reservation even without completion.
    with runtime.session(data) as ledger:
        assert runtime.budget(ledger.state, before_midnight)["used"] == 1
        after_midnight = before_midnight + timedelta(minutes=2)
        second = ledger.reserve(FINGERPRINT, after_midnight)
        assert ledger.state["attempts"][0]["code"] == "model_worker_failed"
        assert runtime.budget(ledger.state, after_midnight)["used"] == 1
        ledger.finish(second, after_midnight, 1)
    assert len(runtime.read_state(data)["attempts"]) == 2


def test_retry_after_prevents_immediate_new_reservation(tmp_path):
    with runtime.session(tmp_path / "data") as ledger:
        attempt = ledger.reserve(FINGERPRINT, NOW)
        ledger.finish(attempt, NOW, 2, error=ModelError("rate_limited", retry_after=60))
        with pytest.raises(ModelError, match="rate_limited"):
            ledger.reserve(FINGERPRINT, NOW + timedelta(seconds=59))
        ledger.reserve(FINGERPRINT, NOW + timedelta(seconds=60))
        assert len(ledger.state["attempts"]) == 2


def test_retention_bounded_and_clock_rollback_fail_closed(tmp_path):
    data = tmp_path / "data"
    with runtime.session(data) as ledger:
        for day in range(35):
            at = NOW + timedelta(days=day)
            for second in range(4):
                at_second = at + timedelta(seconds=second)
                attempt = ledger.reserve(FINGERPRINT, at_second)
                ledger.finish(attempt, at_second, 0)
        assert len(ledger.state["attempts"]) == 120
        with pytest.raises(ModelError, match="model_state_unavailable"):
            ledger.reserve(FINGERPRINT, NOW)
    assert (data / runtime.STATE_FILE).stat().st_size < runtime.MAX_STATE_BYTES


@pytest.mark.parametrize("historical_extra", [0, 21])
def test_full_thirty_day_budget_preserves_history_and_rolls_over(tmp_path, historical_extra):
    data = tmp_path / "data"
    with runtime.session(data) as ledger:
        identity = ledger.reserve(FINGERPRINT, NOW)
        ledger.finish(identity, NOW, 1, usage={"total_tokens": 2**53 - 1})
    template = runtime.read_state(data)["attempts"][0]
    rows = []
    for day in range(30):
        count = 29 if day == 29 else 30 + (historical_extra if day == 0 else 0)
        for second in range(count):
            at = NOW + timedelta(days=day, seconds=second)
            rows.append(
                {
                    **template,
                    "id": f"{len(rows) + 1:032x}",
                    "day": at.astimezone(runtime.ZONE).date().isoformat(),
                    "started_at": at.isoformat(),
                    "finished_at": at.isoformat(),
                }
            )
    runtime._write(data, {"schema_version": 1, "attempts": rows})
    path = data / runtime.STATE_FILE
    assert 128 * 1024 < path.stat().st_size < runtime.MAX_STATE_BYTES
    today = NOW + timedelta(days=29, seconds=60)
    with runtime.session(data) as ledger:
        assert runtime.budget(ledger.state, today) == {"limit": 30, "used": 29, "remaining": 1}
        last = ledger.reserve(FINGERPRINT, today)
        ledger.finish(last, today, 1)
        assert ledger.state["attempts"][:-1] == rows
        assert len(ledger.state["attempts"]) == 900 + historical_extra
        with pytest.raises(ModelError, match="budget_exhausted"):
            ledger.reserve(FINGERPRINT, today)
    before = path.read_bytes()
    assert len(runtime.read_state(data)["attempts"]) == 900 + historical_extra
    assert path.read_bytes() == before  # GET preserves the existing audit, including extras.
    tomorrow = NOW + timedelta(days=30)
    with runtime.session(data) as ledger:
        next_day = ledger.reserve(FINGERPRINT, tomorrow)
        ledger.finish(next_day, tomorrow, 1)
        assert len(ledger.state["attempts"]) == 871
        assert ledger.state["attempts"][0]["day"] == (NOW + timedelta(days=1)).date().isoformat()
        assert runtime.budget(ledger.state, tomorrow) == {"limit": 30, "used": 1, "remaining": 29}


def test_invalid_internal_state_does_not_replace_good_file(tmp_path):
    data = tmp_path / "data"
    with runtime.session(data) as ledger:
        ledger.reserve(FINGERPRINT, NOW)
        path = data / runtime.STATE_FILE
        original = path.read_bytes()
        ledger.state["attempts"][0]["usage"] = {"secret": "not-allowed"}
        with pytest.raises(ModelError, match="model_state_unavailable"):
            runtime._write(data, ledger.state)
        assert path.read_bytes() == original
        assert not list(data.glob("*.tmp"))


def test_over_limit_history_remains_readable_but_normal_admission_stays_closed(
    configured, monkeypatch
):
    clock = datetime.now(UTC)
    with runtime.session(configured.data_dir) as ledger:
        attempt = ledger.reserve(FINGERPRINT, clock)
        ledger.finish(attempt, clock, 1)
    state = runtime.read_state(configured.data_dir)
    template = state["attempts"][0]
    # Synthetic historical facts model a separately authorized, finite live
    # verification. The production reserve API itself has no bypass parameter.
    state["attempts"] = [{**template, "id": f"{i:032x}"} for i in range(1, 52)]
    path = configured.data_dir / runtime.STATE_FILE
    private_file(path, json.dumps(state))
    before = path.read_bytes()
    monkeypatch.setattr(health, "call_model", lambda *_: pytest.fail("No outbound request"))

    restored = runtime.read_state(configured.data_dir)
    assert restored == state
    assert runtime.DAILY_LIMIT == 30
    assert runtime.budget(restored, clock) == {"limit": 30, "used": 51, "remaining": 0}
    assert health.model_status(configured)["budget"] == {
        "limit": 30,
        "used": 51,
        "remaining": 0,
    }
    result = health.run_model_check(configured)
    assert result["attempted"] is False
    assert result["code"] == "budget_exhausted"
    with runtime.session(configured.data_dir) as ledger:
        with pytest.raises(ModelError, match="budget_exhausted"):
            ledger.reserve(FINGERPRINT, clock)
    assert path.read_bytes() == before


@pytest.mark.parametrize("invalid", ["too_many", "extra_field", "duplicate_id", "wrong_day"])
def test_over_limit_history_still_requires_bounded_strict_audit_rows(tmp_path, invalid):
    data = tmp_path / "data"
    with runtime.session(data) as ledger:
        attempt = ledger.reserve(FINGERPRINT, NOW)
        ledger.finish(attempt, NOW, 1)
    state = runtime.read_state(data)
    template = state["attempts"][0]
    count = runtime.MAX_ATTEMPTS + 1 if invalid == "too_many" else 9
    state["attempts"] = [{**template, "id": f"{i:032x}"} for i in range(1, count + 1)]
    if invalid == "extra_field":
        state["attempts"][-1]["purpose"] = "not_in_the_contract"
    elif invalid == "duplicate_id":
        state["attempts"][-1]["id"] = state["attempts"][0]["id"]
    elif invalid == "wrong_day":
        state["attempts"][-1]["day"] = "2000-01-01"
    path = data / runtime.STATE_FILE
    private_file(path, json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(ModelError, match="model_state_unavailable"):
        runtime.read_state(data)
    assert path.read_bytes() == before


def test_interrupted_check_consumes_budget(configured, monkeypatch):
    def interrupt(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr(health, "call_model", interrupt)
    with pytest.raises(KeyboardInterrupt):
        health.run_model_check(configured)
    state = runtime.read_state(configured.data_dir)
    assert state["attempts"][0]["code"] == "model_worker_failed"
    assert health.model_status(configured)["budget"]["used"] == 1


def test_old_running_state_reported_failed_without_repair(configured):
    fingerprint = health.configuration_fingerprint(configured)
    with runtime.session(configured.data_dir) as ledger:
        ledger.reserve(fingerprint, datetime.now(UTC) - timedelta(minutes=5))
    path = configured.data_dir / runtime.STATE_FILE
    before = path.read_bytes()
    status = health.model_status(configured)
    assert status["last_check_status"] == "failed"
    assert status["last_error_code"] == "model_worker_failed"
    assert status["budget"]["used"] == 1
    assert path.read_bytes() == before


def test_unexpected_provider_exception_never_exposes_message(configured, monkeypatch, capsys):
    def failure(*_):
        raise RuntimeError("synthetic-secret-provider-message")

    monkeypatch.setattr(health, "call_model", failure)
    result = health.run_model_check(configured)
    assert result["code"] == "model_check_failed"
    assert "synthetic-secret-provider-message" not in json.dumps(result)
    assert (
        "synthetic-secret-provider-message"
        not in (configured.data_dir / runtime.STATE_FILE).read_text()
    )
    assert capsys.readouterr().out == ""
