"""Budgeted reports with immutable evidence and one successful publication per night."""

import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sleepscape.domain.analysis_readiness import POLICY_VERSION
from sleepscape.domain.daily import instant, window
from sleepscape.domain.sleep_features import SleepAnalysisError
from sleepscape.models.base import OUTPUT_SCHEMA_ERRORS, ChatMessage, ModelError, ModelRequest
from sleepscape.models.openai_compatible import strict_json
from sleepscape.models.settings import (
    configuration_fingerprint,
    configuration_status,
    load_model_settings,
)
from sleepscape.resources import (
    contract_schema,
    prompt_metadata,
    system_prompt,
    validate_model_output,
)
from sleepscape.services.backup import check_restore, create_backup
from sleepscape.services.model_call import call_model
from sleepscape.services.sleep_inputs import read_snapshot
from sleepscape.storage import model_runtime as runtime
from sleepscape.storage import sleep_analysis as store
from sleepscape.storage.reader import DashboardReader, StorageReadError

ZONE = ZoneInfo("Asia/Shanghai")
MAX_NIGHT_ATTEMPTS = 10
MAX_RETRY_WAIT = 30
STOP_ERRORS = frozenset(
    {
        "authentication_failed",
        "permission_denied",
        "model_unavailable",
        "capability_unavailable",
        "model_configuration_invalid",
        "model_key_missing",
        "model_key_invalid",
        "model_key_permissions",
    }
)
RETRY_ERRORS = (
    frozenset(
        {
            "network_error",
            "request_timeout",
            "service_unavailable",
            "rate_limited",
            "model_deadline_exceeded",
            "model_worker_failed",
            "invalid_response",
            "invalid_json",
            "output_schema_mismatch",
            "output_truncated",
            "analysis_output_invalid",
            "analysis_interrupted",
            "output_business_mismatch",
        }
    )
    | OUTPUT_SCHEMA_ERRORS
)
CORRECTION_ERRORS = OUTPUT_SCHEMA_ERRORS | {
    "invalid_json",
    "output_truncated",
    "invalid_response",
    "output_business_mismatch",
}


def prepare_reports(settings):
    """Explicit schema creation after backup and restore validation; no model calls."""
    with DashboardReader(settings.data_dir) as reader:
        if reader.connection is None:
            raise store.SleepAnalysisStorageError("sleep_analysis_not_initialized")
        if store.report_available(reader.connection):
            return {"status": "already_initialized", "schema_version": 1, "backup_verified": False}
    backup = create_backup(settings)
    check_restore(settings, Path(backup["backup_path"]))
    return {**store.migrate_reports(settings.data_dir), "backup_verified": True}


def _time(value):
    return instant(datetime.fromisoformat(value))


def _attempt_limit(rows, clock):
    if any(_time(row["started_at"]) > clock for row in rows):
        raise ModelError("model_state_unavailable")
    # A night's allowance survives date, input, model and configuration changes.
    if len(rows) >= MAX_NIGHT_ATTEMPTS:
        return "blocked_attempts"
    return None


def _public_report(row, changed, reasons):
    source = row["input"]
    payload = source["payload"]
    evidence = {
        key: payload[key]
        for key in ("facts", "timeline", "events", "morning_feeling")
        if key in payload
    }
    return {
        "night_date": row["night_date"],
        "generated_at": row["generated_at"],
        "model_ref": source["model_ref"],
        "prompt_version": source["prompt_version"],
        "input_quality_state": payload["completeness"]["quality_state"],
        "source_changed": changed,
        "source_changed_reasons": reasons,
        "content": {key: value for key, value in row["output"].items() if key != "input_hash"},
        "evidence": evidence,
        "required_limitations": payload["required_limitations"],
    }


def report_state(settings, day, *, readiness=None):
    """GET-safe state; unavailable current data never erases a saved report."""
    result = {
        "execution_state": "not_initialized",
        "report": None,
        "blockers": [],
        "attempt": None,
        "automatic_enabled": settings.llm_automatic_enabled,
    }
    try:
        with DashboardReader(settings.data_dir) as reader:
            if reader.connection is None or not store.report_available(reader.connection):
                result["blockers"] = ["sleep_analysis_not_initialized"]
                return result
            saved = store.read_report(
                settings.data_dir, day.isoformat(), connection=reader.connection
            )
        if saved:
            changed, reasons = None, ["current_data_unavailable"]
            if readiness is not None:
                try:
                    includes_feeling = "morning_feeling" in saved["input"]["payload"]
                    current = read_snapshot(
                        settings,
                        day,
                        build=False,
                        include_morning_feeling=includes_feeling,
                    )
                    if not includes_feeling or current["feeling_state"] == "available":
                        changed = (
                            saved["input"]["source_fingerprint"] != current["source_fingerprint"]
                        )
                        reasons = (
                            [
                                "readiness_policy_changed"
                                if saved["input"]["payload"]["completeness"]["policy_version"]
                                != POLICY_VERSION
                                else "source_data_changed"
                            ]
                            if changed
                            else []
                        )
                except Exception:
                    pass
            result.update(
                execution_state="complete", report=_public_report(saved, changed, reasons)
            )
            return result
        attempts = store.read_attempts(settings.data_dir, day.isoformat())
        if readiness is None:
            result.update(
                execution_state="unavailable", blockers=["analysis_readiness_unavailable"]
            )
            return result
        if readiness["data_state"] != "ready":
            result["execution_state"] = readiness["data_state"]
            return result
        model = configuration_status(settings)
        if model["configuration_state"] != "configured":
            result["execution_state"] = model["configuration_state"]
            result["blockers"] = [model["code"] or "model_not_configured"]
            return result
        snap = read_snapshot(settings, day)
        if snap["input"] is None:
            result["execution_state"] = snap["readiness"]["data_state"]
            return result
        rows = attempts
        clock = datetime.now(UTC)
        result["execution_state"] = _attempt_limit(rows, clock) or "queued"
        if rows:
            latest = rows[-1]
            state = latest["state"]
            if state == "running" and clock - _time(latest["started_at"]) > timedelta(seconds=185):
                state = "failed"
            result["attempt"] = {
                "status": state,
                "error_code": latest["error_code"]
                or (
                    "analysis_interrupted"
                    if latest["state"] == "running" and state == "failed"
                    else None
                ),
                "retry_after": (
                    (
                        _time(latest["finished_at"]) + timedelta(seconds=latest["retry_after"])
                    ).isoformat()
                    if latest["retry_after"] is not None and latest["finished_at"]
                    else None
                ),
                "attempt_count": len(rows),
            }
            if state == "running":
                result["execution_state"] = "running"
            elif result["execution_state"] != "blocked_attempts":
                result["execution_state"] = (
                    state if state in {"running", "failed", "discarded_input_changed"} else "queued"
                )
        if result["execution_state"] == "queued":
            if runtime.budget(runtime.read_state(settings.data_dir), clock)["remaining"] == 0:
                result["execution_state"] = "blocked_budget"
        return result
    except SleepAnalysisError as exc:
        result.update(
            execution_state="blocked_input_budget"
            if exc.code == "blocked_input_budget"
            else "failed",
            blockers=[exc.code],
        )
    except (store.SleepAnalysisStorageError, StorageReadError, ModelError):
        result.update(execution_state="unavailable", blockers=["sleep_analysis_state_unavailable"])
    except Exception:
        result.update(execution_state="unavailable", blockers=["sleep_analysis_state_unavailable"])
    return result


def _request(payload, *, correction=False):
    # The model supplies prose and evidence references only. Trusted report
    # metadata is assembled locally after both wire and business validation.
    schema = contract_schema("model_output")
    instructions = (
        system_prompt()
        + "\n\n模型输出 JSON Schema（完整字段与约束）：\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )
    messages = [
        ChatMessage("system", instructions),
        ChatMessage("user", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        ChatMessage(
            "user",
            "请解释上面的这一晚证据，仅返回以下四个字段组成的JSON对象："
            + "、".join(schema["required"])
            + "。直接以{开头、}结尾，不要包装字符串或数组，也不要只返回observations列表。"
            + "先回答昨晚睡得怎么样，指出哪些证据相互印证或有分歧。"
            + "不写数字、时刻、百分比或证据编号；编号仅放evidence_ids。"
            + "不提供诊断、用药或治疗指令；证据有限时说清具体哪项判断受限。"
            + "比较须同时引用本晚或前日原值；睡眠类须引用时长事实。"
            + "单个evidence_ids只留直接依据，最多六个。它不是整晚证据的目录。"
            + "输出前核对条数：四类观察各一条，假设和建议各零或一条，每条假设一个替代解释。",
        ),
    ]
    if correction:
        messages.append(
            ChatMessage(
                "user",
                "上次输出未通过合同验证。请重新输出规定的四字段JSON对象，摘要先解读本晚表现，包含四类观察与有效证据引用；文字不要写数字或医疗结论，不要输出Markdown或额外字段。",
            )
        )
    return ModelRequest(messages, schema, schema_name="sleepscape_sleep_report")


def _result(day, state, *, code=None, calls=0):
    return {"night_date": day.isoformat(), "state": state, "code": code, "model_calls": calls}


def _process_night(settings, day, ledger, *, now_fn, caller, sleeper, revise=False):
    """Model mutex is held, but no database transaction spans a network request."""
    existing = store.read_report(settings.data_dir, day.isoformat())
    if revise and existing is None:
        return _result(day, "failed", code="sleep_analysis_report_missing")
    if existing and (
        not revise or existing["input"]["prompt_hash"] == prompt_metadata()["prompt_sha256"]
    ):
        return _result(day, "complete")
    for old in store.read_attempts(settings.data_dir, day.isoformat()):
        if old["state"] == "running":
            store.finish_attempt(
                settings.data_dir,
                old["attempt_id"],
                state="abandoned",
                error_code="analysis_interrupted",
                now=now_fn(),
            )
    calls = 0
    correction = False
    max_attempts = 1 if revise else MAX_NIGHT_ATTEMPTS
    for retry in range(max_attempts):
        clock = now_fn()
        snap = read_snapshot(settings, day, now=clock)
        payload = snap["input"]
        if payload is None:
            return _result(day, snap["readiness"]["data_state"], calls=calls)
        night_attempts = store.read_attempts(settings.data_dir, day.isoformat())
        blocked = _attempt_limit(night_attempts, clock)
        if blocked:
            return _result(day, blocked, calls=calls)
        rows = [row for row in night_attempts if row["input_hash"] == payload["input_hash"]]
        model = load_model_settings(settings)
        if not model.enabled:
            return _result(day, "disabled", code="model_disabled", calls=calls)
        config = configuration_fingerprint(settings)
        if rows and rows[-1]["error_code"] in STOP_ERRORS:
            previous = store.read_input(settings.data_dir, rows[-1]["input_id"])
            if previous["config_fingerprint"] == config:
                return _result(day, "failed", code=rows[-1]["error_code"], calls=calls)
        meta = prompt_metadata()
        source = store.save_input(
            settings.data_dir,
            payload,
            source_fingerprint=snap["source_fingerprint"],
            prompt_hash=meta["prompt_sha256"],
            schema_hash=meta["schema_sha256"],
            prompt_version=meta["prompt_version"],
            model_ref=model.model_ref,
            config_fingerprint=config,
            time_mapping_version=snap["time_mapping_version"],
            now=clock,
        )
        request = _request(payload, correction=correction)
        request_id = ledger.reserve(config, clock)
        try:
            attempt = store.begin_attempt(
                settings.data_dir,
                source["input_id"],
                request_id=request_id,
                now=clock,
                revise_attempt=existing["attempt_id"] if revise else None,
            )
        except Exception:
            ledger.finish(request_id, now_fn(), 0, error=ModelError("model_operation_failed"))
            raise
        began = time.monotonic()
        failure = None
        usage = {}
        output = None
        try:
            calls += 1
            reply = caller(model, request)
            usage = reply.usage
            candidate = strict_json(reply.text)
            try:
                output = validate_model_output(candidate, payload)
            except SleepAnalysisError as exc:
                if exc.code == "model_insufficient_input":
                    raise ModelError("model_insufficient_input") from None
                raise ModelError("output_business_mismatch", retryable=True) from None
            if output["status"] != "complete":
                raise ModelError("output_schema_mismatch", retryable=True)
        except ModelError as exc:
            failure = exc
        except (KeyboardInterrupt, SystemExit):
            ledger.finish(
                request_id,
                now_fn(),
                int((time.monotonic() - began) * 1000),
                error=ModelError("model_worker_failed"),
                usage=usage,
            )
            store.finish_attempt(
                settings.data_dir,
                attempt["attempt_id"],
                state="abandoned",
                error_code="analysis_interrupted",
                now=now_fn(),
            )
            raise
        except Exception:
            failure = ModelError("model_operation_failed")
        ledger.finish(
            request_id, now_fn(), int((time.monotonic() - began) * 1000), error=failure, usage=usage
        )
        if failure is not None:
            store.finish_attempt(
                settings.data_dir,
                attempt["attempt_id"],
                state="failed",
                error_code=failure.code,
                retry_after=failure.retry_after,
                now=now_fn(),
            )
            delay = failure.retry_after if failure.retry_after is not None else 1
            eligible_retry = failure.code in RETRY_ERRORS and (
                failure.retryable or failure.code in CORRECTION_ERRORS
            )
            if retry == max_attempts - 1 or not eligible_retry or delay > MAX_RETRY_WAIT:
                return _result(day, "failed", code=failure.code, calls=calls)
            if runtime.budget(ledger.state, now_fn())["remaining"] == 0:
                return _result(day, "blocked_budget", code="budget_exhausted", calls=calls)
            if _attempt_limit(
                store.read_attempts(settings.data_dir, day.isoformat()),
                now_fn(),
            ):
                return _result(day, "failed", code=failure.code, calls=calls)
            sleeper(delay)
            correction = failure.code in CORRECTION_ERRORS
            continue
        try:
            current = read_snapshot(settings, day, now=now_fn())
            unchanged = current["input"] is not None and (
                current["input"]["input_hash"] == payload["input_hash"]
                and current["source_fingerprint"] == snap["source_fingerprint"]
                and configuration_fingerprint(settings) == config
                and prompt_metadata() == meta
            )
        except Exception:
            unchanged = False
        if not unchanged:
            store.finish_attempt(
                settings.data_dir,
                attempt["attempt_id"],
                state="discarded_input_changed",
                error_code="input_changed",
                now=now_fn(),
            )
            return _result(day, "discarded_input_changed", code="input_changed", calls=calls)

        def publish_guard(connection, payload=payload, snap=snap, config=config, meta=meta):
            try:
                fresh = read_snapshot(settings, day, now=now_fn(), connection=connection)
                return fresh["input"] is not None and (
                    fresh["input"]["input_hash"] == payload["input_hash"]
                    and fresh["source_fingerprint"] == snap["source_fingerprint"]
                    and configuration_fingerprint(settings) == config
                    and prompt_metadata() == meta
                )
            except Exception:
                return False

        try:
            store.publish_report(
                settings.data_dir,
                attempt["attempt_id"],
                output,
                now=now_fn(),
                model_metadata={
                    "model": reply.model,
                    "usage": usage,
                    "finish_reason": "stop",
                    "duration_ms": int((time.monotonic() - began) * 1000),
                    **(
                        {"transport_normalization": reply.transport_normalization}
                        if reply.transport_normalization != "none"
                        else {}
                    ),
                },
                guard=publish_guard,
                revise_attempt=existing["attempt_id"] if revise else None,
            )
        except store.SleepAnalysisStorageError as exc:
            if exc.code == "sleep_analysis_already_published":
                store.finish_attempt(
                    settings.data_dir,
                    attempt["attempt_id"],
                    state="discarded_already_published",
                    error_code="already_published",
                    now=now_fn(),
                )
                return _result(day, "complete", calls=calls)
            if exc.code != "sleep_analysis_source_changed":
                raise
            store.finish_attempt(
                settings.data_dir,
                attempt["attempt_id"],
                state="discarded_input_changed",
                error_code="input_changed",
                now=now_fn(),
            )
            return _result(day, "discarded_input_changed", code="input_changed", calls=calls)
        return _result(day, "complete", calls=calls)
    return _result(day, "failed", calls=calls)


def run_analysis(
    settings, *, day=None, automatic=False, now=None, caller=None, sleeper=time.sleep, revise=False
):
    """Never fetch Garmin, implicitly migrate tables or rewrite an existing report."""
    clock = instant(now if now is not None else datetime.now(UTC))
    now_fn = (lambda: clock) if now is not None else (lambda: datetime.now(UTC))
    period = window(clock, settings.garmin_lookback_days)
    first, today = (date.fromisoformat(period[k]) for k in ("start_date", "end_date"))
    if revise and (day is None or automatic):
        raise ValueError("revision_requires_explicit_night")
    if day is not None and (type(day) is not date or not first <= day <= today):
        raise ValueError("analysis_date_outside_window")
    base = {"status": "completed", "automatic": automatic, "model_calls": 0, "results": []}
    if automatic and not settings.llm_automatic_enabled:
        return {**base, "status": "disabled", "code": "automatic_analysis_disabled"}
    config = configuration_status(settings)
    if config["configuration_state"] != "configured":
        return {**base, "status": config["configuration_state"], "code": config["code"]}
    physical_calls = 0

    def counted_call(model, request):
        nonlocal physical_calls
        physical_calls += 1
        return (caller or call_model)(model, request)

    try:
        with DashboardReader(settings.data_dir) as reader:
            if reader.connection is None or not store.report_available(reader.connection):
                return {
                    **base,
                    "status": "not_initialized",
                    "code": "sleep_analysis_not_initialized",
                }
        if revise:
            backup = create_backup(settings)
            check_restore(settings, Path(backup["backup_path"]))
        days = (
            [day]
            if day is not None
            else [today] + [first + timedelta(days=i) for i in range((today - first).days)]
        )
        with runtime.session(settings.data_dir) as ledger:
            for target in days:
                before = physical_calls
                try:
                    result = _process_night(
                        settings,
                        target,
                        ledger,
                        now_fn=now_fn,
                        caller=counted_call,
                        sleeper=sleeper,
                        revise=revise,
                    )
                except SleepAnalysisError as exc:
                    result = _result(
                        target,
                        "blocked_input_budget" if exc.code == "blocked_input_budget" else "failed",
                        code=exc.code,
                    )
                except ModelError as exc:
                    result = _result(
                        target,
                        "blocked_budget" if exc.code == "budget_exhausted" else "failed",
                        code=exc.code,
                    )
                except (store.SleepAnalysisStorageError, StorageReadError):
                    result = _result(target, "failed", code="sleep_analysis_state_unavailable")
                result["model_calls"] = physical_calls - before
                base["results"].append(result)
                if result["code"] in STOP_ERRORS or result["code"] in {
                    "budget_exhausted",
                    "rate_limited",
                }:
                    break
    except ModelError as exc:
        base.update(
            status="blocked_budget" if exc.code == "budget_exhausted" else "failed", code=exc.code
        )
    base["model_calls"] = physical_calls
    if any(item["state"] in {"failed", "discarded_input_changed"} for item in base["results"]):
        base["status"] = "partial"
    return base
