"""Synthetic end-to-end review qualification, publication, and read-only access."""

import json
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_readiness_service import DAY, NOW, complete
from test_readiness_service import data as data
from test_sleep_analysis_service import add_night
from test_sleep_analysis_service import configured as configured
from test_sleep_analysis_service import isolate as isolate

from sleepscape.app import create_app
from sleepscape.domain.morning_feeling import Feeling
from sleepscape.models.base import ModelError, ModelResult
from sleepscape.models.openai_compatible import MAX_MESSAGE_BYTES, _payload
from sleepscape.models.settings import load_model_settings
from sleepscape.services import daily, readiness, sleep_analysis, weekly_review
from sleepscape.services.backup import BackupError, check_restore, create_backup
from sleepscape.services.backup_bundle import create_bundle, validate_bundle
from sleepscape.storage import model_runtime, morning_feeling
from sleepscape.storage import weekly_review as store
from sleepscape.storage.reader import DashboardReader
from sleepscape.web.security import save_password


@pytest.fixture
def prepared(configured, monkeypatch):
    settings, _ = configured
    settings.llm_review_enabled = True
    settings.dashboard_start_date = DAY
    assert weekly_review.prepare_review(settings)["backup_verified"]
    assert weekly_review.prepare_review(settings)["status"] == "already_initialized"

    def forbidden(*args, **kwargs):
        pytest.fail("review must use the explicit synthetic caller")

    monkeypatch.setattr(weekly_review, "call_model", forbidden)
    return configured


def good_reply(_model, request):
    from test_sleep_analysis_service import response_for

    data = json.loads(request.messages[1].content)
    assert set(data) == {"window", "nights"}
    assert data["window"]["end_date"] == data["nights"][-1]["night_date"]
    return response_for(request)


@pytest.mark.parametrize("length", [3, 4, 7])
def test_actual_window_all_nights_checked_and_one_physical_call(prepared, length):
    settings, batch = prepared
    cutoff = DAY + timedelta(days=length - 1)
    now = NOW + timedelta(days=length - 1)
    for day in range(1, length):
        add_night(settings, batch, DAY + timedelta(days=day))
    missing = weekly_review.run_review(settings, cutoff, now=now)
    assert missing["state"] == "incomplete_data"
    assert [row["date"] for row in missing["incomplete"]] == [str(DAY)]
    assert model_runtime.read_state(settings.data_dir)["attempts"] == []
    stored = weekly_review.review_view(settings, cutoff, now=now)
    assert stored["window"]["visible_n"] == length
    assert stored["incomplete"] == missing["incomplete"]
    assert stored["report"] is None

    complete(prepared)
    with DashboardReader(settings.data_dir) as reader:
        before_run = store.load_run(reader.connection, str(cutoff))
    view = weekly_review.review_view(settings, cutoff, now=now)
    assert (view["state"], view["incomplete"], view["report"]) == ("ready", [], None)
    assert model_runtime.read_state(settings.data_dir)["attempts"] == []
    with DashboardReader(settings.data_dir) as reader:
        assert store.load_run(reader.connection, str(cutoff)) == before_run
    morning_feeling.migrate(settings.data_dir)
    morning_feeling.save(settings.data_dir, DAY, Feeling.POOR)
    observed = []

    def caller(model, request):
        evidence = json.loads(request.messages[1].content)
        observed.append(evidence)
        assert (
            sum(len(message.content.encode()) for message in request.messages) <= MAX_MESSAGE_BYTES
        )
        assert b"synthetic-device" not in request.messages[1].content.encode()
        wire, _ = _payload(model, request)
        assert len(wire) < 64 * 1024
        return good_reply(model, request)

    result = weekly_review.run_review(settings, cutoff, now=now, caller=caller)
    assert result == {"state": "complete", "error_code": None, "model_calls": 1}
    (evidence,) = observed
    assert evidence["window"] == {
        "start_date": str(DAY),
        "end_date": str(cutoff),
        "visible_n": length,
        "eligible_n": length,
    }
    assert sum(n["morning_feeling"] is None for n in evidence["nights"]) == length - 1
    assert len(evidence["nights"]) == length
    assert weekly_review.run_review(settings, cutoff, now=now)["model_calls"] == 0
    view = weekly_review.review_view(settings, cutoff, now=now)
    assert view["source_changed"] is False
    assert view["report"]["evidence"] == {**evidence, "target_date": str(cutoff)}
    assert "source_hash" not in json.dumps(view)
    later = now + timedelta(days=40)
    assert weekly_review.review_view(settings, cutoff, now=later)["source_changed"] is False
    morning_feeling.save(settings.data_dir, DAY, Feeling.GOOD)
    assert weekly_review.review_view(settings, cutoff, now=later)["source_changed"] is True
    assert weekly_review.review_view(settings, cutoff, now=now)["source_changed"] is True
    assert weekly_review.run_review(settings, cutoff, now=now)["model_calls"] == 0
    settings.dashboard_start_date = DAY + timedelta(days=1)
    assert weekly_review.review_view(settings, cutoff, now=now)["report"] is None


def test_reply_rejected_or_source_changed_does_not_publish_or_retry(prepared):
    settings, _ = prepared
    complete(prepared)
    morning_feeling.migrate(settings.data_dir)

    def changing(model, request):
        morning_feeling.save(settings.data_dir, DAY, Feeling.POOR)
        return good_reply(model, request)

    result = weekly_review.run_review(settings, DAY, now=NOW, caller=changing)
    assert result["state"] == "source_changed" and result["model_calls"] == 1
    assert weekly_review.review_view(settings, DAY, now=NOW)["report"] is None
    assert weekly_review.run_review(settings, DAY, now=NOW)["model_calls"] == 0


def test_strict_output_rejects_fabricated_references_then_retries_until_shared_cap(prepared):
    settings, _ = prepared
    complete(prepared)

    def invalid(model, request):
        reply = good_reply(model, request)
        content = json.loads(reply.text)
        content["summary"]["evidence_ids"] = ["A99"]
        return ModelResult(json.dumps(content), reply.model, "stop", {})

    with model_runtime.session(settings.data_dir) as ledger:
        for _ in range(model_runtime.DAILY_LIMIT - 2):
            request_id = ledger.reserve("a" * 64, NOW)
            ledger.finish(request_id, NOW, 0)
    result = weekly_review.run_review(
        settings, DAY, now=NOW, caller=invalid, sleeper=lambda _: None
    )
    assert result["state"] == "failed" and result["error_code"] == "output_schema_mismatch"
    assert result["model_calls"] == 2
    assert weekly_review.review_view(settings, DAY, now=NOW)["report"] is None
    assert weekly_review.run_review(settings, DAY, now=NOW)["model_calls"] == 0
    attempts = model_runtime.read_state(settings.data_dir)["attempts"]
    assert len(attempts) == model_runtime.DAILY_LIMIT
    assert [row["code"] for row in attempts[-2:]] == ["output_schema_mismatch"] * 2


def test_migrates_a_legacy_failure_and_retries_with_correction(prepared):
    settings, _ = prepared
    complete(prepared)
    with store.connection(settings.data_dir) as db, db:
        db.execute("DROP TRIGGER sleep_review_run_terminal")
        db.execute("DROP TABLE sleep_review_failed_attempts")
        db.execute(store._DDL["sleep_review_run_terminal"])
        assert store.schema_version(db) == 1
    with DashboardReader(settings.data_dir) as reader:
        evidence, source, _ = weekly_review._snapshot(settings, reader, [DAY], NOW)
    store.claim(settings.data_dir, evidence, source, "a" * 64, "b" * 64, NOW.isoformat())
    with model_runtime.session(settings.data_dir) as ledger:
        first = ledger.reserve("a" * 64, NOW)
        ledger.finish(first, NOW, 0, error=ModelError("output_schema_max_items"))
    store.attach_request(settings.data_dir, str(DAY), first)
    store.finish(settings.data_dir, str(DAY), "failed", "output_schema_max_items")
    assert weekly_review.prepare_review(settings) == {
        "status": "upgraded",
        "schema_version": 2,
        "backup_verified": True,
    }
    failures = []

    def caller(model, request):
        assert "JSON Schema" in request.messages[-1].content
        if not failures:
            failures.append(True)
            raise ModelError("output_schema_max_items")
        return good_reply(model, request)

    result = weekly_review.run_review(settings, DAY, now=NOW, caller=caller, sleeper=lambda _: None)
    assert result == {"state": "complete", "error_code": None, "model_calls": 2}
    assert weekly_review.run_review(settings, DAY, now=NOW)["model_calls"] == 0
    with DashboardReader(settings.data_dir) as reader:
        store.validate(reader.connection)
        rows = reader.connection.execute(
            "SELECT request_id,error_code FROM sleep_review_failed_attempts ORDER BY rowid"
        ).fetchall()
        assert len(rows) == 2 and rows[0][0] == first
        assert [row[1] for row in rows] == ["output_schema_max_items"] * 2
    backup = create_backup(settings)
    check_restore(settings, Path(backup["backup_path"]))


def test_lock_budget_cooldown_deadline_and_crash(prepared):
    settings, _ = prepared
    complete(prepared)
    assert (
        weekly_review.run_review(settings, DAY, now=NOW, deadline=time.monotonic())["error_code"]
        == "review_deadline"
    )
    with model_runtime.session(settings.data_dir) as ledger:
        assert weekly_review.run_review(settings, DAY, now=NOW)["error_code"] == "model_busy"
        rid = ledger.reserve("a" * 64, NOW)
        ledger.finish(rid, NOW, 0, error=ModelError("rate_limited", retry_after=60))
    assert weekly_review.run_review(settings, DAY, now=NOW)["error_code"] == "rate_limited"
    allowed = NOW + timedelta(minutes=2)

    def interrupted(*args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        weekly_review.run_review(settings, DAY, now=allowed, caller=interrupted)
    assert weekly_review.run_review(settings, DAY, now=allowed)["state"] == "interrupted"
    assert len(model_runtime.read_state(settings.data_dir)["attempts"]) == 2


def test_review_backup_restore_and_readonly_route(prepared, tmp_path):
    settings, _ = prepared
    settings.web_auth_file = tmp_path / ".synthetic-private" / "web-auth"
    save_password(settings.web_auth_file, "synthetic", "synthetic-review-secret")
    complete(prepared)
    assert (
        weekly_review.run_review(settings, DAY, now=NOW, caller=good_reply)["state"] == "complete"
    )
    endpoint = f"/api/v1/sleep-reviews/{DAY}"
    app = create_app(settings)
    db = settings.data_dir / "sleepscape.sqlite3"
    before = db.read_bytes(), model_runtime.read_state(settings.data_dir)
    with TestClient(app, base_url="https://localhost", client=("192.0.2.9", 443)) as unauth:
        assert unauth.get(endpoint).status_code == 401
        assert unauth.post(endpoint).status_code == 405
        auth = ("synthetic", "synthetic-review-secret")
        assert unauth.get("/sleep", auth=auth).status_code == 200
        response = unauth.get(endpoint)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["report"]["content"]["summary"]["text"].startswith("这晚")
        assert unauth.get("/api/v1/sleep-reviews/2025-12-31").status_code == 404
    assert before == (db.read_bytes(), model_runtime.read_state(settings.data_dir))
    backup = create_backup(settings)
    backup_path = settings.data_dir / Path(backup["backup_path"])
    restored = check_restore(settings, backup_path)
    assert restored["counts"]["sleep_review_reports"] == 1
    bundle = create_bundle(settings, "a" * 32, NOW)
    assert bundle["status"] == "verified"
    folder = settings.data_dir / "backups" / "managed" / bundle["id"]
    assert validate_bundle(folder, restore=True)["counts"]["sleep_review_reports"] == 1
    with sqlite3.connect(backup_path) as tampered:
        tampered.execute("DROP TRIGGER sleep_review_reports_no_update")
    with pytest.raises(BackupError, match="backup_sleep_review_schema_invalid"):
        check_restore(settings, backup_path)


def test_daily_appends_review_after_existing_stages_and_keeps_result(prepared, monkeypatch):
    settings, _ = prepared
    observed = []

    def mark(name, result):
        def run(*args, **kwargs):
            observed.append(name)
            if name == "review":
                assert args[1] == DAY and kwargs["deadline"] > time.monotonic()
            return result

        return run

    collection = {"action": "executed", "run": {"day": str(DAY)}, "status": "completed"}
    check = {"status": "checked", "window": {}, "counts": {}, "persisted": True}
    monkeypatch.setattr(daily, "_run_daily", mark("collection", collection))
    monkeypatch.setattr(readiness, "check_readiness", mark("readiness", check))
    monkeypatch.setattr(sleep_analysis, "run_analysis", mark("nightly", {"status": "completed"}))
    monkeypatch.setattr(weekly_review, "run_review", mark("review", {"state": "failed"}))
    result = daily.run_daily(settings, now=NOW)
    assert observed == ["collection", "readiness", "nightly", "review"]
    assert result["status"] == result["analysis"] == "completed"
    assert result["review"]["state"] == "failed"


def test_publish_barrier_checks_concurrent_commits_after_guard_snapshot(prepared, monkeypatch):
    settings, _ = prepared
    complete(prepared)
    morning_feeling.migrate(settings.data_dir)
    snapshot = weekly_review._snapshot
    committed = []

    def read_then_change(settings, reader, days, clock):
        result = snapshot(settings, reader, days, clock)
        if reader.connection.execute("PRAGMA query_only").fetchone()[0] == 0 and not committed:
            morning_feeling.save(settings.data_dir, DAY, Feeling.POOR)
            committed.append(True)
        return result

    monkeypatch.setattr(weekly_review, "_snapshot", read_then_change)
    result = weekly_review.run_review(settings, DAY, now=NOW, caller=good_reply)
    assert committed == [True]
    assert result["state"] == "source_changed"
    assert weekly_review.review_view(settings, DAY, now=NOW)["report"] is None


def test_empty_and_disabled_and_shared_daily_limit(prepared):
    settings, _ = prepared
    settings.llm_review_enabled = False
    assert weekly_review.run_review(settings, DAY, now=NOW)["state"] == "disabled"
    settings.llm_review_enabled = True
    assert (
        weekly_review.run_review(settings, DAY - timedelta(days=1), now=NOW)["state"]
        == "empty_range"
    )
    complete(prepared)
    with model_runtime.session(settings.data_dir) as ledger:
        for _ in range(model_runtime.DAILY_LIMIT):
            identifier = ledger.reserve("1" * 64, NOW)
            ledger.finish(identifier, NOW, 1)
    assert weekly_review.run_review(settings, DAY, now=NOW)["error_code"] == "budget_exhausted"
    with DashboardReader(settings.data_dir) as reader:
        row = store.load_run(reader.connection, str(DAY))
        assert row["state"] == "blocked" and row["request_id"] is None
    assert len(model_runtime.read_state(settings.data_dir)["attempts"]) == model_runtime.DAILY_LIMIT


def test_larger_valid_input_and_openai_transport(prepared):
    from sleepscape.resources.weekly_review import request_for

    settings, batch = prepared
    complete(prepared)
    days = [DAY + timedelta(days=i) for i in range(7)]
    for day in days[1:]:
        add_night(settings, batch, day)
    with DashboardReader(settings.data_dir) as reader:
        evidence, _, _ = weekly_review._snapshot(settings, reader, days, NOW + timedelta(days=6))
    assert list(n["night_date"] for n in evidence["nights"]) == list(map(str, days))
    assert evidence["target_date"] == str(days[-1])
    assert set(evidence) == {"target_date", "window", "nights"}
    request = request_for(evidence)

    assert json.loads(request.messages[1].content) == {k: evidence[k] for k in ("window", "nights")}
    assert "整个观测窗口" in request.messages[0].content
    assert "解读目标夜的睡眠" not in request.messages[0].content
    assert len(request.messages[1].content.encode()) <= 24 * 1024
    assert sum(len(message.content.encode()) for message in request.messages) <= MAX_MESSAGE_BYTES
    raw, _ = _payload(load_model_settings(settings), request)
    assert len(raw) <= 64 * 1024


def test_review_rolls_independently_of_garmin_lookback(prepared):
    settings, batch = prepared
    settings.garmin_lookback_days = 1
    for offset in range(1, 8):
        add_night(settings, batch, DAY + timedelta(days=offset))
    end = DAY + timedelta(days=7)
    now = NOW + timedelta(days=7)
    result = weekly_review.run_review(settings, end, now=now, caller=good_reply)
    assert result["state"] == "complete"
    evidence = weekly_review.review_view(settings, end, now=now)["report"]["evidence"]
    assert evidence["window"]["start_date"] == str(DAY + timedelta(days=1))
    assert evidence["window"]["visible_n"] == 7


@pytest.mark.parametrize("feeling", [None, Feeling.GOOD])
def test_daily_publishes_both_report_types_on_the_same_ledger(prepared, monkeypatch, feeling):
    from test_sleep_analysis_service import good_caller

    settings, _ = prepared
    settings.garmin_lookback_days = 1
    complete(prepared)
    assert readiness.prepare_readiness(settings)["backup_verified"]
    assert sleep_analysis.prepare_reports(settings)["backup_verified"]
    morning_feeling.migrate(settings.data_dir)
    morning_feeling.save(settings.data_dir, DAY - timedelta(days=1), Feeling.POOR)
    if feeling is not None:
        morning_feeling.save(settings.data_dir, DAY, feeling)
    monkeypatch.setattr(
        daily,
        "_run_daily",
        lambda *args, **kwargs: {
            "action": "executed",
            "status": "completed",
            "run": {"day": str(DAY)},
        },
    )
    prompts = []

    def nightly_caller(model, request):
        prompts.append("nightly")
        evidence = json.loads(request.messages[1].content)
        assert "filled_at" not in request.messages[1].content
        response = good_caller(model, request)
        assert evidence["nights"][0]["morning_feeling"] == feeling
        return response

    def review_caller(model, request):
        prompts.append("review")
        evidence = json.loads(request.messages[1].content)
        assert evidence["nights"][0]["morning_feeling"] == feeling
        assert "filled_at" not in request.messages[1].content
        return good_reply(model, request)

    monkeypatch.setattr(sleep_analysis, "call_model", nightly_caller)
    monkeypatch.setattr(weekly_review, "call_model", review_caller)

    result = daily.run_daily(settings, now=NOW)
    assert result["reports"]["results"][0]["state"] == "complete"
    assert result["review"]["state"] == "complete"
    assert result["reports"]["model_calls"] == result["review"]["model_calls"] == 1
    ledger = model_runtime.read_state(settings.data_dir)["attempts"]
    assert len(ledger) == 2 and len({row["id"] for row in ledger}) == 2
    assert ledger[0]["fingerprint"] == ledger[1]["fingerprint"]
    assert prompts == ["nightly", "review"]
    nightly = sleep_analysis.report_state(
        settings, DAY, readiness=readiness.evaluate_night(settings, DAY, now=NOW)
    )["report"]
    assert ("morning_feeling" in nightly["evidence"]) == (feeling is not None)
    assert (nightly["content"]["feeling"] is not None) == (feeling is not None)
    assert weekly_review.review_view(settings, DAY, now=NOW)["report"] is not None
