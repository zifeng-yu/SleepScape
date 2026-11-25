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
from sleepscape.resources.weekly_review import validate_evidence
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
    evidence = validate_evidence(json.loads(request.messages[1].content))
    result = {
        "summary": {"text": "这段时间手表记录的典型睡眠时长很短。", "evidence_ids": ["A1"]},
        "observations": [
            {"text": "这晚的记录可以回看。", "evidence_ids": [evidence["examples"][0]]}
        ],
        "hypotheses": [],
        "actions": [],
    }
    return ModelResult(
        json.dumps(result, ensure_ascii=False), "qwen3.8-max", "stop", {"total_tokens": 1}
    )


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
    assert evidence["quality"]["feeling_counts"]["unfilled"] == length - 1
    assert len(evidence["nights"]) == length
    assert weekly_review.run_review(settings, cutoff, now=now)["model_calls"] == 0
    view = weekly_review.review_view(settings, cutoff, now=now)
    assert view["source_changed"] is False
    assert view["report"]["evidence"] == evidence
    assert "source_hash" not in json.dumps(view)
    morning_feeling.save(settings.data_dir, DAY, Feeling.GOOD)
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


def test_new_review_summary_focus_preserves_historical_read(prepared):
    from sleepscape.resources.weekly_review import parse_output, request_for, validate_output

    settings, _ = prepared
    complete(prepared)
    with DashboardReader(settings.data_dir) as reader:
        evidence, _, _ = weekly_review._snapshot(settings, reader, [DAY], NOW)
    reply = json.loads(good_reply(None, request_for(evidence)).text)
    temperature = next(row for row in evidence["aggregates"] if row["metric"] == "temperature_c")
    for summary in (
        {"text": "本周采样完整度较高，可进行睡眠分析。", "evidence_ids": evidence["examples"]},
        {"text": "这段时间睡得很安稳。", "evidence_ids": [temperature["id"]]},
    ):
        reply["summary"] = summary
        validate_output(reply, evidence)
        with pytest.raises(ModelError, match="output_business_mismatch"):
            parse_output(json.dumps(reply), evidence)


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
    assert result["state"] == "failed" and result["error_code"] == "output_business_mismatch"
    assert result["model_calls"] == 2
    assert weekly_review.review_view(settings, DAY, now=NOW)["report"] is None
    assert weekly_review.run_review(settings, DAY, now=NOW)["model_calls"] == 0
    attempts = model_runtime.read_state(settings.data_dir)["attempts"]
    assert len(attempts) == model_runtime.DAILY_LIMIT
    assert [row["code"] for row in attempts[-2:]] == ["output_business_mismatch"] * 2


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
        assert "上次输出未通过校验" in request.messages[0].content
        assert "actions 有依据时保留一条" in request.messages[0].content
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
        assert response.json()["report"]["content"]["summary"]["evidence_ids"] == ["A1"]
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
    from sleepscape.domain.weekly_review import build_evidence
    from sleepscape.resources.weekly_review import request_for

    settings, _ = prepared
    complete(prepared)
    with DashboardReader(settings.data_dir) as reader:
        payload, _, _ = weekly_review._snapshot(settings, reader, [DAY], NOW)
    row = payload["nights"][0]
    rows = [
        {
            **row,
            "id": f"N{i + 1}",
            "night_date": str(DAY + timedelta(days=i)),
            "environment_group": f"G{i + 1}",
            "feeling": ("good", "poor")[i % 2],
        }
        for i in range(7)
    ]
    evidence = build_evidence(rows, "available")
    assert len(evidence["aggregates"]) == 34
    assert all(item["environment_group"] is None for item in evidence["comparisons"])
    assert len(evidence["comparisons"]) == 6
    assert "environment_groups_differ" in evidence["quality"]["limits"]
    request = request_for(evidence)
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
        if feeling is None:
            assert "morning_feeling" not in evidence
            return response
        assert evidence["morning_feeling"] == {"id": "F000", "value": "good"}
        report = json.loads(response.text)
        assert "F000" in report["summary"]["evidence_ids"]
        return ModelResult(json.dumps(report), response.model, "stop", response.usage)

    def review_caller(model, request):
        prompts.append("review")
        evidence = json.loads(request.messages[1].content)
        assert evidence["nights"][0]["feeling"] == feeling
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
    assert ("F000" in nightly["content"]["summary"]["evidence_ids"]) == (feeling is not None)
    assert weekly_review.review_view(settings, DAY, now=NOW)["report"] is not None


def test_with_gaps_and_low_light_flags_are_preserved():
    from zoneinfo import ZoneInfo

    from test_analysis_readiness import DAY, NOW, evaluate, light_bus_gap, sample

    from sleepscape.domain.weekly_review import nightly_evidence

    night, environment = sample(minutes=3)
    light = light_bus_gap(environment, 0, 1)
    light.update(min=3, mean=3, max=3)
    sound = environment["records"][0]["metrics"]["sound_relative"]
    sound.update(
        state="partial",
        valid_count=59,
        valid_1s_count=59,
        valid_duration_ms=59000,
        valid_samples=944000,
        quality=["partial_window", "audio_gap"],
    )
    result = evaluate(night, environment)
    assert result["core_ready"] and result["quality_state"] == "with_gaps"
    evidence, _ = nightly_evidence(
        DAY, night, environment, result, now=NOW, zone=ZoneInfo("Asia/Shanghai")
    )
    assert "low_light_limit" in evidence["limits"]
    assert "light_bus_sampling_gap" in evidence["limits"]
    assert evidence["metrics"]["illuminance_lux"] == pytest.approx(3 * 59 / 179)
    assert evidence["metrics"]["sound_relative"] == pytest.approx(-45)


def test_cross_configuration_comparison_in_free_text_is_rejected():
    from sleepscape.domain.weekly_review import build_evidence
    from sleepscape.models.base import ModelError
    from sleepscape.resources.weekly_review import validate_output

    nights = [
        {
            "id": identifier,
            "night_date": str(DAY + timedelta(days=offset)),
            "metrics": {"sleep_seconds": 120, "temperature_c": 20 + offset},
            "environment_group": group,
            "feeling": None,
            "limits": [],
        }
        for offset, (identifier, group) in enumerate((("N1", "G1"), ("N2", "G2")))
    ]
    evidence = build_evidence(nights, "available")
    payload = {
        "summary": {
            "text": "后一个夜晚的温度更高。",
            "evidence_ids": ["N1", "N2"],
        },
        "observations": [{"text": "可回看这晚。", "evidence_ids": ["N1"]}],
        "hypotheses": [],
        "actions": [],
    }
    with pytest.raises(ModelError, match="output_business_mismatch"):
        validate_output(payload, evidence)


def test_duration_matched_reversal_is_replayed_and_required_in_new_prose(prepared):
    from sleepscape.domain.weekly_review import build_evidence, pair_checks
    from sleepscape.resources.weekly_review import parse_output, request_for, validate_output

    settings, _ = prepared
    complete(prepared)
    with DashboardReader(settings.data_dir) as reader:
        snapshot, _, _ = weekly_review._snapshot(settings, reader, [DAY], NOW)
    seed = snapshot["nights"][0]
    nights = [
        {
            **seed,
            "id": f"N{index}",
            "night_date": str(DAY + timedelta(days=index - 1)),
            "feeling": feeling,
            "environment_group": group,
            "metrics": {
                **seed["metrics"],
                "sleep_seconds": length,
                "sleep_end_offset_seconds": wake,
            },
        }
        for index, (length, wake, feeling, group) in enumerate(
            (
                (8100, 100, "poor", "G1"),
                (8100, 200, "fair", "G2"),
                (8000, 300, "poor", "G1"),
                (8200, 200, "fair", "G1"),
            ),
            1,
        )
    ]
    source = build_evidence(nights, "available")
    validate_evidence(source)
    by_metric = {r["metric"]: r for r in source["pair_checks"]}
    wake = by_metric["sleep_end_offset_seconds"]
    assert wake["counterexample"]
    assert wake["pair_n"] == 3 and wake["night_n"] == 4
    assert by_metric["temperature_c"]["night_n"] == 2
    reply = json.loads(good_reply(None, request_for(source)).text)
    reply["summary"] = {"text": wake["text"], "evidence_ids": [wake["id"]]}
    assert parse_output(json.dumps(reply), source) == reply
    # Original evidence may lack the derived cards and its old prose remains readable.
    legacy = {k: v for k, v in source.items() if k != "pair_checks"}
    reply["summary"] = {"text": "醒得较早的夜晚晨起睡感更好。", "evidence_ids": source["examples"]}
    validate_output(reply, legacy)
    for refs in ([wake["id"]], source["examples"]):
        reply["summary"]["evidence_ids"] = refs
        with pytest.raises(ModelError, match="output_business_mismatch"):
            parse_output(json.dumps(reply), source)
    reply["summary"] = {"text": wake["text"], "evidence_ids": [wake["id"]]}
    reply["actions"] = [
        {
            "text": "下周尝试提前起床。",
            "observe": "留意后续几晚。",
            "evidence_ids": source["examples"],
        }
    ]
    with pytest.raises(ModelError, match="output_business_mismatch"):
        parse_output(json.dumps(reply), source)
    reply["actions"][0].update(text="记录醒来时间和晨起睡感。", observe="留意次日的感受。")
    parse_output(json.dumps(reply), source)
    reply["actions"][0]["observe"] += "醒得早的夜晚睡感也好。"
    with pytest.raises(ModelError, match="output_business_mismatch"):
        parse_output(json.dumps(reply), source)
    wake["night_n"] = 2
    with pytest.raises(ModelError):
        validate_evidence(source)
    single = pair_checks(nights[:2], ["N2", "N1"])
    single_wake = next(r for r in single if r["metric"] == "sleep_end_offset_seconds")
    assert single_wake["pair_n"] == 1 and "仅有这对" in single_wake["text"]
    assert single_wake["counterexample"] == [] and single_wake["max_duration_gap_seconds"] == 0
    assert not any(r["metric"] == "temperature_c" for r in single)
    assert not pair_checks([{**n, "feeling": None} for n in nights], [])


def test_pair_card_survives_publication_read_and_repeat(prepared):
    settings, batch = prepared
    end = DAY + timedelta(days=1)
    clock = NOW + timedelta(days=1)
    add_night(settings, batch, end)
    complete(prepared)
    morning_feeling.migrate(settings.data_dir)
    morning_feeling.save(settings.data_dir, DAY, Feeling.FAIR)
    morning_feeling.save(settings.data_dir, end, Feeling.POOR)

    def caller(model, request):
        source = json.loads(request.messages[1].content)
        proof = next(r for r in source["pair_checks"] if r["metric"] == "sleep_end_offset_seconds")
        reply = good_reply(model, request)
        output = json.loads(reply.text)
        output["summary"] = {"text": proof["text"], "evidence_ids": [proof["id"]]}
        return ModelResult(json.dumps(output), reply.model, "stop", reply.usage)

    result = weekly_review.run_review(settings, end, now=clock, caller=caller)
    assert result["state"] == "complete" and result["model_calls"] == 1
    view = weekly_review.review_view(settings, end, now=clock)
    assert view["source_changed"] is False
    stored = view["report"]
    proof = next(
        r
        for r in stored["evidence"]["pair_checks"]
        if r["id"] == stored["content"]["summary"]["evidence_ids"][0]
    )
    assert (proof["pair_n"], proof["night_n"], proof["counterexample"]) == (1, 2, [])
    assert stored["content"]["summary"]["text"] == proof["text"]
    assert weekly_review.run_review(settings, end, now=clock)["model_calls"] == 0
    backup = create_backup(settings)
    check_restore(settings, Path(backup["backup_path"]))


def test_weekly_observations_match_the_cited_night_and_median_limitations(prepared):
    from sleepscape.domain.weekly_review import build_evidence
    from sleepscape.resources.weekly_review import parse_output, request_for

    settings, _ = prepared
    complete(prepared)
    with DashboardReader(settings.data_dir) as reader:
        base, _, _ = weekly_review._snapshot(settings, reader, [DAY], NOW)
    for feeling in ("poor", None):
        source = build_evidence([{**base["nights"][0], "feeling": feeling}], "available")
        candidate = json.loads(good_reply(None, request_for(source)).text)
        for claim in ("这晚睡眠时长有记录，晨起睡感较好。", "这晚晨起睡感较好。"):
            candidate["observations"][0] = {"text": claim, "evidence_ids": ["N1"]}
            with pytest.raises(ModelError, match="output_business_mismatch"):
                parse_output(json.dumps(candidate), source)
        if feeling == "poor":
            candidate["observations"][0] = {"text": "晨起睡感较差。", "evidence_ids": ["A1"]}
            with pytest.raises(ModelError, match="output_business_mismatch"):
                parse_output(json.dumps(candidate), source)
            candidate["observations"][0]["evidence_ids"] = ["N1"]
            parse_output(json.dumps(candidate), source)
        candidate["actions"] = [
            {
                "text": "下次可以记录晨起睡感是否较好。",
                "observe": "留意自己醒来的感受。",
                "evidence_ids": ["N1"],
            }
        ]
        candidate["observations"][0] = {
            "text": "仅凭中位数无法判断整段是否稳定。",
            "evidence_ids": ["A1"],
        }
        parse_output(json.dumps(candidate), source)
        candidate["observations"][0]["text"] += "整段都很稳定。"
        with pytest.raises(ModelError, match="output_business_mismatch"):
            parse_output(json.dumps(candidate), source)


def test_generation_replays_aggregates_groups_and_examples_before_using_directions(prepared):
    from copy import deepcopy

    from sleepscape.domain.weekly_review import build_evidence
    from sleepscape.resources.weekly_review import request_for

    settings, _ = prepared
    complete(prepared)
    with DashboardReader(settings.data_dir) as reader:
        snapshot, _, _ = weekly_review._snapshot(settings, reader, [DAY], NOW)
    seed = snapshot["nights"][0]
    nights = [
        {
            **seed,
            "id": f"N{i + 1}",
            "night_date": str(DAY + timedelta(days=i)),
            "feeling": rating,
            "metrics": {**seed["metrics"], "sleep_seconds": 100 + i},
        }
        for i, rating in enumerate(("poor", "good", "poor", "good"))
    ]
    source = build_evidence(nights, "available")
    request_for(source, correction=True)
    forged = deepcopy(source)
    forged["examples"] = [n["id"] for n in nights if n["id"] not in source["examples"]][:2]
    with pytest.raises(ModelError, match="model_operation_failed"):
        request_for(forged)
    for group, field in (
        ("aggregates", "median"),
        ("comparisons", "difference"),
        ("comparisons", "poor_median"),
    ):
        forged = deepcopy(source)
        forged[group][0][field] += 1
        with pytest.raises(ModelError, match="model_operation_failed"):
            request_for(forged)
    historical = {k: v for k, v in source.items() if k != "pair_checks"}
    validate_evidence(historical)
    with pytest.raises(ModelError):
        request_for(historical)


@pytest.mark.parametrize("same_group", [True, False])
def test_review_claims_require_the_named_metric_and_comparable_nights(prepared, same_group):
    from sleepscape.domain.weekly_review import build_evidence
    from sleepscape.resources.weekly_review import parse_output, request_for

    settings, _ = prepared
    complete(prepared)
    with DashboardReader(settings.data_dir) as reader:
        first = weekly_review._snapshot(settings, reader, [DAY], NOW)[0]["nights"][0]
    second = {**first, "id": "N2", "night_date": str(DAY + timedelta(days=1))}
    nights = [
        first,
        {**second, "environment_group": first["environment_group"] if same_group else "G2"},
    ]
    source = build_evidence(nights, "available")
    candidate = json.loads(good_reply(None, request_for(source)).text)
    aggregate = {row["metric"]: row["id"] for row in source["aggregates"]}

    def check(text, ids):
        candidate["observations"][0] = {"text": text, "evidence_ids": ids}
        parse_output(json.dumps(candidate), source)

    for text, ids in (
        ("这晚的卧室温度与其他夜晚不同。", ["N1"]),
        ("这几晚的卧室温度一直很稳定。", ["N1"]),
        ("卧室温度有记录。和其他夜晚不一样。", ["N1"]),
        ("各晚的心率和睡眠时长都可回看。", [aggregate["sleep_seconds"]]),
        ("这几晚的卧室温度一直很稳定。", ["N1", aggregate["sleep_seconds"]]),
    ):
        with pytest.raises(ModelError, match="output_business_mismatch"):
            check(text, ids)
    with pytest.raises(ModelError, match="output_business_mismatch"):
        check("这两晚的温度不同。", ["N1", "N2"])
    check("这晚的卧室温度有记录。", ["N1"])
    check(
        "可回看夜间心率与睡眠时长的中位数。",
        [aggregate["average_heart_rate"], aggregate["sleep_seconds"]],
    )
    check("只凭该晚的温度无法判断其他夜晚是否稳定。", ["N1"])
    check("仅凭这晚的卧室温度无法判断是否与其他夜晚不同。", ["N1"])
    text = "这两晚的卧室温度记录可以互相对照。"
    if same_group:
        check(text, ["N1", "N2"])
        direct = ["N1", aggregate["temperature_c"]]
        check("这晚的卧室温度与整段中位数相同。", direct)
        with pytest.raises(ModelError, match="output_business_mismatch"):
            check("这晚的卧室温度高于整段中位数。", direct)
        with pytest.raises(ModelError, match="output_business_mismatch"):
            check("两晚的卧室温度不相同。", ["N1", "N2"])
        nights[1]["metrics"] = {
            **first["metrics"],
            "temperature_c": first["metrics"]["temperature_c"] + 1,
        }
        source = build_evidence(nights, "available")
        check("两晚的卧室温度不相同。", ["N1", "N2"])
        check("这晚的卧室温度低于整段中位数。", direct)
        check("这晚的卧室温度不等于整段中位数。", direct)
        with pytest.raises(ModelError, match="output_business_mismatch"):
            check("这晚的卧室温度不低于整段中位数。", direct)
        check("整段卧室温度中位数高于这晚的温度。", direct)
        with pytest.raises(ModelError, match="output_business_mismatch"):
            check("这晚的卧室温度高于整段中位数。", direct)
        with pytest.raises(ModelError, match="output_business_mismatch"):
            check("两晚的卧室温度相同。", ["N1", "N2"])
    else:
        with pytest.raises(ModelError, match="output_business_mismatch"):
            check("这晚的卧室温度与整段中位数相同。", ["N1", aggregate["temperature_c"]])
        with pytest.raises(ModelError, match="output_business_mismatch"):
            check(text, ["N1", "N2"])
