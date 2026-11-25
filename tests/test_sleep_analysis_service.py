"""Real synthetic SQLite -> evidence -> fake provider -> guarded one-report publication."""

import copy
import json
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.request import OpenerDirector

import pytest
import requests
from fastapi.testclient import TestClient
from test_readiness_service import DAY, NOW, complete
from test_readiness_service import data as readiness_data

from sleepscape.app import create_app
from sleepscape.domain.environment import EnvironmentBatch
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.domain.morning_feeling import Feeling
from sleepscape.models.base import OUTPUT_SCHEMA_ERRORS, ModelError, ModelResult
from sleepscape.models.openai_compatible import MAX_MESSAGE_BYTES, _payload
from sleepscape.models.settings import configuration_fingerprint, load_model_settings
from sleepscape.resources import (
    contract_schema,
    prompt_metadata,
    validate_model_output,
    validate_sleep_input,
)
from sleepscape.services import daily, readiness
from sleepscape.services import sleep_analysis as service
from sleepscape.services.sleep_inputs import read_snapshot
from sleepscape.storage import environment, morning_feeling
from sleepscape.storage import model_runtime as runtime
from sleepscape.storage import sleep_analysis as store
from sleepscape.storage.daily_runtime import collection_busy
from sleepscape.storage.daily_runtime import read_state as daily_state
from sleepscape.storage.repository import Repository

# Reuse the established two-minute source fixture; all resulting health data are authored fake data.
data = readiness_data


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper().startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        pytest.fail("Synthetic report tests must never contact a real service")

    monkeypatch.setattr(OpenerDirector, "open", forbidden)
    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr(service, "call_model", forbidden)


@pytest.fixture
def configured(data, tmp_path):
    settings, batch = data
    private = tmp_path / ".synthetic-private"
    private.mkdir(mode=0o700)
    key = private / "model-key"
    key.write_text("synthetic-test-key-never-sent")
    key.chmod(0o600)
    settings.llm_enabled = True
    settings.llm_automatic_enabled = True
    settings.llm_base_url = "https://synthetic-provider.example/v1"
    settings.llm_key_file = key
    return settings, batch


@pytest.fixture
def prepared(configured):
    complete(configured)
    settings, _ = configured
    assert service.prepare_reports(settings)["backup_verified"]
    return configured


def response_for(request):
    """Read the real anonymous evidence from the request; satisfy the actual business validator."""
    payload = json.loads(next(m.content for m in request.messages if m.role == "user"))
    validate_sleep_input(payload)
    ids = {row["metric"]: row["id"] for row in payload["facts"]}
    refs = {
        "sleep": [ids["sleep.total_minutes"]],
        "body_signals": [ids["sleep.average_heart_rate"], ids["sleep.night_average_hrv"]],
        "previous_day": [ids["previous.training_load"], ids["previous.average_stress"]],
        "environment": [ids["environment.temperature_mean"], ids["environment.sound_energy_mean"]],
    }
    result = {
        "summary": {
            "text": "手表只记下一小段睡眠，单凭时长还说不清醒来时的感受。",
            "evidence_ids": refs["sleep"].copy(),
        },
        "observations": [
            {
                "category": category,
                "text": "环境有缺采，难以判断缺口中的声音变化。"
                if category == "environment" and payload["completeness"]["warnings"]
                else "已有该类观测，仅描述提供的证据；暂不推断因果关系。",
                "evidence_ids": evidence,
            }
            for category, evidence in refs.items()
        ],
        "hypotheses": [],
        "actions": [],
    }
    if feeling := payload.get("morning_feeling"):
        label = {"good": "较好", "fair": "一般", "poor": "较差"}[feeling["value"]]
        result["summary"]["text"] = f"晨起睡感{label}。" + result["summary"]["text"]
        result["summary"]["evidence_ids"].append(feeling["id"])
    validate_model_output(result, payload)
    return ModelResult(
        json.dumps(result, ensure_ascii=False), "qwen3.8-max", "stop", {"total_tokens": 40}
    )


def good_caller(_model, request):
    return response_for(request)


@pytest.mark.parametrize("correction", [False, True])
def test_schema_visible_in_prompt_and_maximum_input_fits_messages(configured, correction):
    settings, _ = configured
    payload = {"synthetic_padding": ""}
    overhead = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    payload["synthetic_padding"] = "x" * (24 * 1024 - overhead)
    request = service._request(payload, correction=correction)
    schema = contract_schema("model_output")
    assert request.output_schema == schema
    encoded_schema = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    assert request.messages[0].content.endswith(encoded_schema)
    assert all(field in request.messages[2].content for field in schema["required"])
    assert set(schema["required"]) == {"summary", "observations", "hypotheses", "actions"}
    assert not any(
        field in encoded_schema for field in ("input_hash", "input_quality_state", "evidence_level")
    )
    assert sum(len(message.content.encode()) for message in request.messages) <= MAX_MESSAGE_BYTES
    wire, original = _payload(load_model_settings(settings), request)
    assert original == schema and len(wire) < 64 * 1024


def run(settings, **kwargs):
    return service.run_analysis(settings, day=DAY, now=NOW, caller=good_caller, **kwargs)


def counts(settings):
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
        return {
            name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("analysis_inputs", "analysis_attempts", "sleep_analysis_reports")
        }


def update_hrv(settings, *, day=DAY, value=65):
    with Repository(settings.data_dir) as repo:
        task = repo.start_run({"synthetic": True})
        request = DataRequest("hrv", str(day), "/synthetic", start_date=str(day), end_date=str(day))
        rows = [
            {
                "kind": "hrv_daily",
                "key": str(day),
                "date": str(day),
                "timestamp": None,
                "data": {"night_average_ms": value, "quality": {}},
            }
        ]
        repo.save_snapshot(
            task,
            FetchResult(request, {"synthetic": True, "revision": value}, NOW.isoformat(), 200),
            NormalizedBatch(rows),
        )
        repo.finish_run(task, "completed", {})


def add_night(settings, batch, day):
    """Another ready night in the same real database, with a separate boot and known UTC."""
    start = datetime.combine(day, datetime.min.time(), UTC)
    previous = day - timedelta(days=1)
    with Repository(settings.data_dir) as repo:
        task = repo.start_run({"synthetic": True})
        for resource, selected, rows, metrics in (
            (
                "sleep",
                day,
                [
                    (
                        "sleep",
                        {
                            "start_at": start.isoformat(),
                            "end_at": (start + timedelta(minutes=2)).isoformat(),
                            "sleep_seconds": 120,
                            "average_heart_rate": 55,
                            "confirmed": True,
                            "stage_timeline_available": False,
                        },
                    )
                ],
                {},
            ),
            ("hrv", day, [("hrv_daily", {"night_average_ms": 50})], {}),
            ("activities", previous, [], {"pagination_complete": True}),
            ("daily_summaries", previous, [("daily_summary", {"average_stress": 0})], {}),
        ):
            req = DataRequest(
                resource,
                str(selected),
                "/synthetic",
                start_date=str(selected),
                end_date=str(selected),
            )
            normalized = [
                {
                    "kind": kind,
                    "key": str(selected),
                    "date": str(selected),
                    "timestamp": None,
                    "data": {"quality": {}, **values},
                }
                for kind, values in rows
            ]
            repo.save_snapshot(
                task,
                FetchResult(
                    req,
                    {"synthetic": True, "day": str(selected), "resource": resource},
                    NOW.isoformat(),
                    200,
                ),
                NormalizedBatch(normalized, metrics=metrics),
            )
        repo.finish_run(task, "completed", {})
    payload = copy.deepcopy(batch)
    payload["boot_id"] = "synthetic-" + str(day)
    payload["time_anchors"][0]["utc_at"] = start.isoformat().replace("+00:00", "Z")
    second = copy.deepcopy(payload["records"][0])
    second.update(seq=1, mono_start_ms=60000, mono_end_ms=120000)
    payload["records"].append(second)
    environment.ingest(
        settings.data_dir, payload["device_id"], EnvironmentBatch.model_validate(payload)
    )


def save_pending(settings, *, now=NOW):
    snap = read_snapshot(settings, DAY, now=now)
    meta = prompt_metadata()
    config = configuration_fingerprint(settings)
    source = store.save_input(
        settings.data_dir,
        snap["input"],
        source_fingerprint=snap["source_fingerprint"],
        prompt_hash=meta["prompt_sha256"],
        schema_hash=meta["schema_sha256"],
        prompt_version=meta["prompt_version"],
        model_ref="sleep_analyst",
        config_fingerprint=config,
        time_mapping_version=snap["time_mapping_version"],
        now=now,
    )
    with runtime.session(settings.data_dir) as ledger:
        request_id = ledger.reserve(config, now)
    return store.begin_attempt(
        settings.data_dir, source["input_id"], request_id=request_id, now=now
    )


def test_explicit_prepare_checks_recoverable_backup_and_is_idempotent(configured):
    settings, _ = configured
    first = service.prepare_reports(settings)
    assert first == {"status": "initialized", "schema_version": 1, "backup_verified": True}
    backups = list((settings.data_dir / "backups").rglob("*.sqlite3"))
    assert len(backups) == 1
    second = service.prepare_reports(settings)
    assert second == {
        "status": "already_initialized",
        "schema_version": 1,
        "backup_verified": False,
    }
    assert counts(settings) == {
        "analysis_inputs": 0,
        "analysis_attempts": 0,
        "sleep_analysis_reports": 0,
    }
    assert not (settings.data_dir / runtime.STATE_FILE).exists()


def test_real_evidence_unique_publication_and_repeat_is_zero_call(prepared):
    settings, _ = prepared
    calls = []

    def caller(model, request):
        calls.append(request)
        assert model.model == "qwen3.8-max"
        assert not collection_busy(settings.data_dir)
        with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3", timeout=0) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        assert "synthetic-device" not in request.messages[1].content
        assert "synthetic-profile" not in request.messages[1].content
        assert "synthetic-test-key" not in request.messages[1].content
        return response_for(request)

    result = service.run_analysis(settings, day=DAY, now=NOW, caller=caller)
    assert result["results"][0]["state"] == "complete"
    assert result["model_calls"] == 1
    first = store.read_report(settings.data_dir, str(DAY))
    result = service.run_analysis(settings, day=DAY, now=NOW + timedelta(minutes=1), caller=caller)
    assert result["model_calls"] == 0 and result["results"][0]["state"] == "complete"
    assert len(calls) == 1
    assert store.read_report(settings.data_dir, str(DAY)) == first
    assert counts(settings) == {
        "analysis_inputs": 1,
        "analysis_attempts": 1,
        "sleep_analysis_reports": 1,
    }
    assert runtime.budget(runtime.read_state(settings.data_dir), NOW)["used"] == 1


def test_report_metadata_is_assembled_from_trusted_input_without_rewriting_model_prose(prepared):
    settings, _ = prepared
    responses = []

    def caller(_model, request):
        reply = response_for(request)
        responses.append(json.loads(reply.text))
        return reply

    result = service.run_analysis(settings, day=DAY, now=NOW, caller=caller)
    assert result["results"][0]["state"] == "complete"
    report = store.read_report(settings.data_dir, str(DAY))
    source, output = report["input"]["payload"], report["output"]
    assert output["schema_version"] == "sleep-output-1" and output["status"] == "complete"
    assert output["night_date"] == source["night_date"]
    assert output["input_hash"] == source["input_hash"]
    assert output["input_quality_state"] == source["completeness"]["quality_state"]
    assert output["limitations"] == source["required_limitations"]
    assert output["summary"] == responses[0]["summary"]
    assert output["actions"] == responses[0]["actions"]
    assert output["hypotheses"] == responses[0]["hypotheses"]
    assert [
        {key: value for key, value in row.items() if key != "evidence_level"}
        for row in output["observations"]
    ] == responses[0]["observations"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "sleep-output-1"),
        ("night_date", "2000-01-01"),
        ("input_hash", "a" * 64),
        ("input_quality_state", "complete"),
        ("status", "complete"),
        ("limitations", []),
    ],
)
def test_wire_cannot_supply_internal_metadata_even_if_other_content_is_valid(
    prepared, field, value
):
    settings, _ = prepared

    def caller(_model, request):
        reply = response_for(request)
        candidate = json.loads(reply.text)
        candidate[field] = value
        return replace(reply, text=json.dumps(candidate))

    result = service.run_analysis(settings, day=DAY, now=NOW, caller=caller, sleeper=lambda _: None)
    assert result["results"][0]["state"] == "failed"
    assert result["results"][0]["code"] == "output_business_mismatch"
    assert result["model_calls"] == 10
    assert counts(settings)["sleep_analysis_reports"] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["summary"].update(evidence_ids=["F999"]),
        lambda value: value["summary"].update(text="心率为99，这是未经证据支持的数字。"),
        lambda value: value["observations"].pop(),
        lambda value: value.update(observations=[]),
        lambda value: value["observations"][0].update(evidence_level="moderate"),
        lambda value: value["summary"]["evidence_ids"].append(value["summary"]["evidence_ids"][0]),
    ],
)
def test_invalid_wire_references_text_and_categories_are_rejected_without_repair(prepared, mutate):
    settings, _ = prepared

    def caller(_model, request):
        reply = response_for(request)
        candidate = json.loads(reply.text)
        mutate(candidate)
        return replace(reply, text=json.dumps(candidate))

    result = service.run_analysis(settings, day=DAY, now=NOW, caller=caller, sleeper=lambda _: None)
    assert result["results"][0]["state"] == "failed"
    assert result["results"][0]["code"] == "output_business_mismatch"
    assert result["model_calls"] == 10
    assert counts(settings)["sleep_analysis_reports"] == 0


def test_get_report_never_writes_calls_or_exposes_private_metadata(prepared, monkeypatch):
    settings, _ = prepared
    run(settings)
    monkeypatch.setattr(
        "sleepscape.services.analysis_views.evaluate_night",
        lambda *_: readiness.evaluate_night(settings, DAY, now=NOW),
    )
    before = counts(settings), (settings.data_dir / runtime.STATE_FILE).read_bytes()
    with TestClient(
        create_app(settings), base_url="http://localhost", client=("127.0.0.1", 5000)
    ) as client:
        for _ in range(2):
            response = client.get(f"/api/v1/nights/{DAY}/analysis")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["report"]["content"]["status"] == "complete"
            assert body["report"]["source_changed"] is False
            night = client.get(f"/api/v1/nights/{DAY}").json()
            assert night["published_summary"] == body["report"]["content"]["summary"]["text"]
            for private in (
                "input_hash",
                "source_fingerprint",
                "prompt_sha256",
                "synthetic-provider",
                "synthetic-test-key",
                "device_id",
                "payload_json",
            ):
                assert private not in response.text
        assert client.post(f"/api/v1/nights/{DAY}/analysis").status_code == 405
    assert before == (counts(settings), (settings.data_dir / runtime.STATE_FILE).read_bytes())


def test_generation_source_revision_discards_candidate_without_overwriting_facts(prepared):
    settings, _ = prepared

    def changed(_model, request):
        update_hrv(settings)
        return response_for(request)

    result = service.run_analysis(settings, day=DAY, now=NOW, caller=changed)
    assert result["results"][0]["state"] == "discarded_input_changed"
    assert store.read_report(settings.data_dir, str(DAY)) is None
    assert store.read_attempts(settings.data_dir, str(DAY))[0]["state"] == "discarded_input_changed"
    fresh = read_snapshot(settings, DAY, now=NOW)["input"]
    assert (
        next(row["value"] for row in fresh["facts"] if row["metric"] == "sleep.night_average_hrv")
        == 65
    )


def test_revision_between_preflight_and_publication_is_rejected_by_real_transaction_guard(
    prepared, monkeypatch
):
    settings, _ = prepared
    publish = store.publish_report
    guards = []

    def race(data_dir, attempt_id, output, **kwargs):
        update_hrv(settings)
        guard = kwargs["guard"]

        def tracked(connection):
            assert connection.in_transaction
            guards.append(True)
            return guard(connection)

        return publish(data_dir, attempt_id, output, **{**kwargs, "guard": tracked})

    monkeypatch.setattr(store, "publish_report", race)
    result = run(settings)
    assert guards == [True]
    assert result["results"][0]["state"] == "discarded_input_changed"
    assert store.read_report(settings.data_dir, str(DAY)) is None


def test_insufficient_data_has_no_attempt_or_model_request(configured):
    settings, _ = configured
    service.prepare_reports(settings)
    result = run(settings)
    assert result["results"][0]["state"] == "waiting_data" and result["model_calls"] == 0
    assert counts(settings) == {
        "analysis_inputs": 0,
        "analysis_attempts": 0,
        "sleep_analysis_reports": 0,
    }
    assert runtime.budget(runtime.read_state(settings.data_dir), NOW)["used"] == 0


def test_model_refusal_error_is_recorded_without_coercive_retry(prepared):
    settings, _ = prepared
    calls = []

    def refused(_model, request):
        calls.append(1)
        raise ModelError("model_insufficient_input")

    result = service.run_analysis(settings, day=DAY, now=NOW, caller=refused)
    assert len(calls) == result["model_calls"] == 1
    assert result["results"][0]["code"] == "model_insufficient_input"
    assert store.read_report(settings.data_dir, str(DAY)) is None


@pytest.mark.parametrize("normalization", ["json_string_envelope", "single_object_array"])
def test_report_records_bounded_transport_normalization_metadata(prepared, normalization):
    settings, _ = prepared

    def normalized(_model, request):
        return replace(response_for(request), transport_normalization=normalization)

    result = service.run_analysis(settings, day=DAY, now=NOW, caller=normalized)
    assert result["results"][0]["state"] == "complete"
    report = store.read_report(settings.data_dir, str(DAY))
    assert report["model_metadata"]["transport_normalization"] == normalization


@pytest.mark.parametrize("code", sorted(OUTPUT_SCHEMA_ERRORS | {"output_business_mismatch"}))
def test_safe_validation_reason_is_audited_and_one_correction_can_succeed(prepared, code):
    settings, _ = prepared
    calls = []

    def sequence(_model, request):
        calls.append(request)
        if len(calls) == 1:
            raise ModelError(code)
        return response_for(request)

    result = service.run_analysis(
        settings, day=DAY, now=NOW, caller=sequence, sleeper=lambda _: None
    )
    assert result["model_calls"] == 2 and result["results"][0]["state"] == "complete"
    attempts = store.read_attempts(settings.data_dir, str(DAY))
    assert next(row for row in attempts if row["state"] == "failed")["error_code"] == code
    assert runtime.read_state(settings.data_dir)["attempts"][0]["code"] == code
    assert "上次输出未通过" in calls[-1].messages[-1].content


def test_last_allowed_attempt_preserves_failure_instead_of_returning_successful_block(prepared):
    settings, _ = prepared
    for index in range(9):
        started = NOW - timedelta(seconds=30 - index * 2)
        prior = save_pending(settings, now=started)
        store.finish_attempt(
            settings.data_dir,
            prior["attempt_id"],
            state="failed",
            error_code="capability_unavailable",
            now=started + timedelta(seconds=1),
        )
    # A real configuration correction unblocks STOP_ERRORS without deleting its audit.
    settings.llm_base_url = "https://synthetic-provider-updated.example/v1"

    def failed(*_):
        raise ModelError("output_schema_contains")

    result = service.run_analysis(settings, day=DAY, now=NOW, caller=failed, sleeper=lambda _: None)
    assert result["status"] == "partial" and result["model_calls"] == 1
    assert result["results"][0]["state"] == "failed"
    assert result["results"][0]["code"] == "output_schema_contains"
    assert len(store.read_attempts(settings.data_dir, str(DAY))) == 10


@pytest.mark.parametrize(
    "failure", ["network", "invalid_json", "wrong_references", "authentication"]
)
def test_retry_is_bounded_and_corrections_do_not_reuse_untrusted_output(prepared, failure):
    settings, _ = prepared
    calls, delays = [], []

    def sequence(_model, request):
        calls.append(request)
        if len(calls) > 1:
            assert all(
                "DO_NOT_FOLLOW_UNTRUSTED" not in message.content for message in request.messages
            )
            return response_for(request)
        if failure == "network":
            raise ModelError("network_error", retryable=True)
        if failure == "authentication":
            raise ModelError("authentication_failed")
        if failure == "invalid_json":
            return ModelResult("DO_NOT_FOLLOW_UNTRUSTED", "qwen3.8-max", "stop", {})
        value = json.loads(response_for(request).text)
        value["summary"]["evidence_ids"] = ["F999"]
        return ModelResult(json.dumps(value), "qwen3.8-max", "stop", {})

    result = service.run_analysis(
        settings, day=DAY, now=NOW, caller=sequence, sleeper=delays.append
    )
    if failure == "authentication":
        assert len(calls) == result["model_calls"] == 1 and delays == []
        assert result["results"][0]["state"] == "failed"
        retry = service.run_analysis(settings, day=DAY, now=NOW, caller=sequence)
        assert retry["model_calls"] == 0
    else:
        assert len(calls) == result["model_calls"] == 2 and delays == [1]
        assert result["results"][0]["state"] == "complete"
        if failure != "network":
            assert len(calls[-1].messages) == 4
    assert runtime.budget(runtime.read_state(settings.data_dir), NOW)["used"] == len(calls)


def test_429_long_retry_after_defers_and_persists_global_cooldown(prepared):
    settings, _ = prepared
    calls, sleeps = [], []

    def limited(*_):
        calls.append(1)
        raise ModelError("rate_limited", retryable=True, retry_after=120)

    first = service.run_analysis(settings, day=DAY, now=NOW, caller=limited, sleeper=sleeps.append)
    assert first["model_calls"] == 1 and sleeps == []
    repeated = service.run_analysis(
        settings, day=DAY, now=NOW + timedelta(seconds=30), caller=limited
    )
    assert repeated["model_calls"] == 0 and len(calls) == 1
    assert repeated["results"][0]["code"] == "rate_limited"
    assert store.read_attempts(settings.data_dir, str(DAY))[0]["retry_after"] == 120
    result = service.run_analysis(
        settings, day=DAY, now=NOW + timedelta(seconds=121), caller=good_caller
    )
    assert result["model_calls"] == 1 and result["results"][0]["state"] == "complete"


def test_shared_daily_budget_counts_prior_model_checks_and_limits_retry(prepared):
    settings, _ = prepared
    with runtime.session(settings.data_dir) as ledger:
        for _ in range(29):
            reserved = ledger.reserve(configuration_fingerprint(settings), NOW)
            ledger.finish(reserved, NOW, 1, usage={"total_tokens": 3})

    def unavailable(*_):
        raise ModelError("network_error", retryable=True)

    result = service.run_analysis(
        settings,
        day=DAY,
        now=NOW,
        caller=unavailable,
        sleeper=lambda _: pytest.fail("no budget for retry"),
    )
    assert result["model_calls"] == 1 and result["results"][0]["state"] == "blocked_budget"
    assert runtime.budget(runtime.read_state(settings.data_dir), NOW)["used"] == 30
    repeated = service.run_analysis(settings, day=DAY, now=NOW, caller=good_caller)
    assert repeated["model_calls"] == 0


def test_night_attempt_limit_survives_new_runs_days_and_changed_inputs(prepared, monkeypatch):
    settings, _ = prepared

    def unavailable(*_):
        raise ModelError("network_error", retryable=True)

    result = service.run_analysis(
        settings, day=DAY, now=NOW, caller=unavailable, sleeper=lambda _: None
    )
    assert result["model_calls"] == 10
    attempts = store.read_attempts(settings.data_dir, str(DAY))
    assert len(attempts) == 10 and len({row["input_hash"] for row in attempts}) == 1
    assert all(row["state"] == "failed" for row in attempts)
    for offset in (0, 1):
        at = NOW + timedelta(days=offset)
        repeated = service.run_analysis(settings, day=DAY, now=at, caller=good_caller)
        assert (
            repeated["model_calls"] == 0 and repeated["results"][0]["state"] == "blocked_attempts"
        )
    update_hrv(settings)
    blocked = service.run_analysis(
        settings, day=DAY, now=NOW + timedelta(days=2), caller=good_caller
    )
    assert blocked["model_calls"] == 0 and blocked["results"][0]["state"] == "blocked_attempts"
    assert store.read_attempts(settings.data_dir, str(DAY)) == attempts
    current = readiness.evaluate_night(settings, DAY, now=NOW + timedelta(days=2))
    monkeypatch.setattr(
        service,
        "read_snapshot",
        lambda settings, day: read_snapshot(settings, day, now=NOW + timedelta(days=2)),
    )
    state = service.report_state(settings, DAY, readiness=current)
    assert state["execution_state"] == "blocked_attempts"
    assert state["attempt"]["attempt_count"] == 10


def test_current_then_oldest_ready_nights_are_not_blocked_by_old_two_night_limit(prepared):
    settings, batch = prepared
    early, middle = DAY - timedelta(days=6), DAY - timedelta(days=3)
    for value in (early, middle):
        add_night(settings, batch, value)
    called = []

    def caller(model, request):
        called.append(json.loads(request.messages[1].content)["night_date"])
        return response_for(request)

    result = service.run_analysis(settings, now=NOW, caller=caller)
    assert called == [str(DAY), str(early), str(middle)]
    assert result["model_calls"] == 3
    later = service.run_analysis(settings, day=middle, now=NOW, caller=caller)
    assert later["model_calls"] == 0 and later["results"][0]["state"] == "complete"


@pytest.mark.parametrize("successful_call", [1, 10])
def test_success_stops_remaining_night_attempts_and_later_execution(prepared, successful_call):
    settings, _ = prepared
    calls, delays = [], []

    def caller(_model, request):
        calls.append(request)
        if len(calls) < successful_call:
            raise ModelError("output_schema_required")
        return response_for(request)

    result = service.run_analysis(settings, day=DAY, now=NOW, caller=caller, sleeper=delays.append)
    assert result["model_calls"] == successful_call
    assert result["results"][0]["state"] == "complete"
    assert len(delays) == successful_call - 1
    assert all(len(request.messages) == 4 for request in calls[1:])
    attempts = store.read_attempts(settings.data_dir, str(DAY))
    assert len(attempts) == successful_call
    assert sum(row["state"] == "failed" for row in attempts) == successful_call - 1
    assert counts(settings)["sleep_analysis_reports"] == 1
    repeated = service.run_analysis(settings, day=DAY, now=NOW + timedelta(days=1), caller=caller)
    assert repeated["model_calls"] == 0
    assert repeated["results"][0]["state"] == "complete"
    assert len(calls) == successful_call
    assert store.read_attempts(settings.data_dir, str(DAY)) == attempts


def test_changed_input_shares_remaining_night_attempts(prepared):
    settings, _ = prepared

    def one_failure(*_):
        raise ModelError("network_error", retryable=False)

    first = service.run_analysis(settings, day=DAY, now=NOW, caller=one_failure)
    assert first["model_calls"] == 1
    update_hrv(settings)

    def failed(*_):
        raise ModelError("output_schema_required")

    second = service.run_analysis(
        settings,
        day=DAY,
        now=NOW + timedelta(days=1),
        caller=failed,
        sleeper=lambda _: None,
    )
    assert second["model_calls"] == 9
    assert second["results"][0]["state"] == "failed"
    assert second["results"][0]["code"] == "output_schema_required"
    attempts = store.read_attempts(settings.data_dir, str(DAY))
    assert len(attempts) == 10
    assert len({row["input_hash"] for row in attempts}) == 2
    assert runtime.budget(runtime.read_state(settings.data_dir), NOW + timedelta(days=1)) == {
        "limit": 30,
        "used": 9,
        "remaining": 21,
    }


def test_three_failed_nights_share_thirty_calls_and_fourth_is_deferred(prepared):
    settings, batch = prepared
    older = [DAY - timedelta(days=offset) for offset in (6, 5, 4)]
    for day in older:
        add_night(settings, batch, day)
    calls = []

    def failed(_model, request):
        calls.append(json.loads(request.messages[1].content)["night_date"])
        raise ModelError("output_schema_required")

    result = service.run_analysis(settings, now=NOW, caller=failed, sleeper=lambda _: None)
    assert result["model_calls"] == len(calls) == 30
    assert calls == [str(DAY)] * 10 + [str(older[0])] * 10 + [str(older[1])] * 10
    assert runtime.budget(runtime.read_state(settings.data_dir), NOW) == {
        "limit": 30,
        "used": 30,
        "remaining": 0,
    }
    assert counts(settings)["analysis_attempts"] == 30
    assert counts(settings)["sleep_analysis_reports"] == 0
    repeated = service.run_analysis(settings, day=older[2], now=NOW, caller=failed)
    assert repeated["model_calls"] == 0
    assert repeated["results"][0]["state"] == "blocked_budget"
    assert repeated["results"][0]["code"] == "budget_exhausted"
    assert store.read_attempts(settings.data_dir, str(older[2])) == []
    assert len(calls) == 30


def test_interrupted_reserved_attempt_survives_restart_and_counts_toward_limit(prepared):
    settings, _ = prepared
    pending = save_pending(settings)
    result = service.run_analysis(
        settings, day=DAY, now=NOW + timedelta(minutes=4), caller=good_caller
    )
    assert result["model_calls"] == 1 and result["results"][0]["state"] == "complete"
    attempts = store.read_attempts(settings.data_dir, str(DAY))
    assert len(attempts) == 2
    abandoned = next(row for row in attempts if row["attempt_id"] == pending["attempt_id"])
    assert abandoned["state"] == "abandoned" and abandoned["error_code"] == "analysis_interrupted"
    assert (
        runtime.budget(runtime.read_state(settings.data_dir), NOW + timedelta(minutes=4))["used"]
        == 2
    )


def test_source_changed_keeps_original_report_and_unknown_current_data_also_keeps_it(prepared):
    settings, _ = prepared
    run(settings)
    original = store.read_report(settings.data_dir, str(DAY))
    current = readiness.evaluate_night(settings, DAY, now=NOW)
    assert (
        service.report_state(settings, DAY, readiness=current)["report"]["source_changed"] is False
    )
    update_hrv(settings)
    fresh = service.report_state(settings, DAY, readiness=current)
    assert fresh["report"]["source_changed"] is True
    assert fresh["report"]["content"] == {
        k: v for k, v in original["output"].items() if k != "input_hash"
    }
    unknown = service.report_state(settings, DAY, readiness=None)
    assert unknown["report"]["source_changed"] is None
    settings.llm_enabled = False
    assert service.report_state(settings, DAY, readiness=current)["execution_state"] == "complete"
    assert store.read_report(settings.data_dir, str(DAY)) == original


@pytest.mark.parametrize("recorded", [False, True])
def test_morning_feeling_edits_preserve_published_report_and_historical_inputs(prepared, recorded):
    settings, _ = prepared
    morning_feeling.migrate(settings.data_dir)
    if recorded:
        morning_feeling.save(settings.data_dir, DAY, Feeling.GOOD)
    assert run(settings)["model_calls"] == 1
    original = store.read_report(settings.data_dir, str(DAY))
    current = readiness.evaluate_night(settings, DAY, now=NOW)
    assert (
        service.report_state(settings, DAY, readiness=current)["report"]["source_changed"] is False
    )

    morning_feeling.save(settings.data_dir, DAY, Feeling.POOR)
    report = service.report_state(settings, DAY, readiness=current)["report"]
    assert report["source_changed"] is recorded
    expected = {"id": "F000", "value": "good"} if recorded else None
    assert report["evidence"].get("morning_feeling") == expected
    assert run(settings)["model_calls"] == 0
    assert store.read_report(settings.data_dir, str(DAY)) == original


def test_automatic_switch_is_separate_from_explicit_run(prepared):
    settings, _ = prepared
    settings.llm_automatic_enabled = False
    skipped = run(settings, automatic=True)
    assert skipped["status"] == "disabled" and skipped["model_calls"] == 0
    assert counts(settings)["analysis_attempts"] == 0
    assert run(settings)["results"][0]["state"] == "complete"


def test_daily_releases_collection_lock_and_failed_report_does_not_undo_environment_ack(
    prepared, monkeypatch
):
    settings, batch = prepared
    readiness.prepare_readiness(settings)
    calls = []

    def collected(settings, *args, **kwargs):
        assert kwargs["_lock_held"] and collection_busy(settings.data_dir)
        with Repository(settings.data_dir) as repo:
            task = repo.start_run(kwargs["metadata"])
            kwargs["on_start"](task)
            summary = {
                "status": "completed",
                "completed_resources": 1,
                "failed_resources": 0,
                "http_requests": 1,
            }
            repo.finish_run(task, "completed", summary)
        return summary

    def failed_model(*_):
        assert not collection_busy(settings.data_dir)
        payload = copy.deepcopy(batch)
        payload["records"][0].update(seq=2, mono_start_ms=120000, mono_end_ms=180000)
        ack = environment.ingest(
            settings.data_dir, payload["device_id"], EnvironmentBatch.model_validate(payload)
        )
        assert ack["records"][0]["status"] == "accepted"
        calls.append(ack)
        raise ModelError("authentication_failed")

    monkeypatch.setattr(daily, "collect", collected)
    monkeypatch.setattr(service, "call_model", failed_model)
    result = daily.run_daily(settings, now=NOW)
    assert result["status"] == "completed"
    assert result["reports"]["status"] == "partial"
    assert calls
    assert daily_state(settings.data_dir)["days"][-1]["status"] == "completed"
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM environment_records WHERE record_type='minute'"
            ).fetchone()[0]
            == 3
        )
    assert store.read_report(settings.data_dir, str(DAY)) is None


def test_manual_new_prompt_preserves_old_report_until_valid_replacement(prepared, monkeypatch):
    settings, _ = prepared
    assert run(settings)["model_calls"] == 1
    old = store.read_report(settings.data_dir, str(DAY))
    updated = prompt_metadata() | {"prompt_sha256": "1" * 64}
    monkeypatch.setattr(service, "prompt_metadata", lambda: updated)

    def fail(*args):
        raise ModelError("invalid_json", retryable=True)

    failed = service.run_analysis(settings, day=DAY, now=NOW, caller=fail, revise=True)
    assert failed["status"] == "partial" and failed["model_calls"] == 1
    assert store.read_report(settings.data_dir, str(DAY)) == old
    assert run(settings)["model_calls"] == 0
    assert run(settings, revise=True)["model_calls"] == 1
    revised = store.read_report(settings.data_dir, str(DAY))
    assert revised["attempt_id"] != old["attempt_id"]
    assert revised["input"]["prompt_hash"] == updated["prompt_sha256"]
    assert len(store.read_attempts(settings.data_dir, str(DAY))) == 3
    assert run(settings, revise=True)["model_calls"] == 0
    backup = service.create_backup(settings)
    from pathlib import Path

    service.check_restore(settings, Path(backup["backup_path"]))
