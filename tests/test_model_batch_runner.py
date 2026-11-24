"""Offline safety, staging and resume checks for the explicitly opted-in live suite."""

import copy
import json
from types import SimpleNamespace

import pytest
from model_batch_cases import generate_cases
from model_batch_runner import BatchEvaluationError, BatchEvaluator

from sleepscape.models.base import ModelError, ModelResult


@pytest.fixture(scope="module")
def batch_cases():
    return generate_cases()


def wire_for(case):
    source = case["payload"]
    ids = {row["metric"]: row["id"] for row in source["facts"]}
    refs = {
        "sleep": [ids["sleep.total_minutes"]],
        "body_signals": [ids["sleep.average_heart_rate"], ids["sleep.night_average_hrv"]],
        "previous_day": [ids["previous.training_load"], ids["previous.average_stress"]],
        "environment": [ids["environment.temperature_mean"], ids["environment.sound_energy_mean"]],
    }
    return {
        "summary": {
            "text": "手表估计的睡眠时长只能说明睡了多久，晨起感受还要结合本人体验。",
            "evidence_ids": refs["sleep"],
        },
        "observations": [
            {
                "category": category,
                "text": "环境有缺采，难以判断缺口中的声音变化。"
                if category == "environment"
                and source["completeness"]["quality_state"] == "with_gaps"
                else "已有该类观测，仅说明所给证据，不推断因果。",
                "evidence_ids": evidence,
            }
            for category, evidence in refs.items()
        ],
        "hypotheses": [],
        "actions": [],
    }


def result_for(case):
    return ModelResult(
        json.dumps(wire_for(case), ensure_ascii=False),
        "qwen3.8-max",
        "stop",
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )


def good_invoke(case, _request):
    return result_for(case)


def test_fifty_unique_inputs_share_frozen_template_and_never_repeat(batch_cases):
    invoked, saved = [], []

    def invoke(case, request):
        invoked.append((case["case_id"], request))
        assert json.loads(request.messages[1].content) == case["payload"]
        return result_for(case)

    evaluator = BatchEvaluator(batch_cases, invoke, sink=saved.append)
    for index in range(50):
        assert evaluator.run_case(index)["business_pass"] is True
        assert evaluator.run_case(index)["business_pass"] is True
    assert len(invoked) == len(saved) == 50
    assert len({row["input_hash"] for row in saved}) == 50
    assert len({row["template_sha256"] for row in saved}) == 1
    assert len({request.messages[0].content for _, request in invoked}) == 1
    assert len({request.messages[2].content for _, request in invoked}) == 1
    summary = evaluator.summary()
    assert summary["business_pass_count"] == summary["schema_pass_count"] == 50
    assert summary["business_pass_rate_of_requested"] == 1
    assert summary["known_usage"]["total_tokens"] == 6000
    assert summary["cases_with_known_usage"] == 50


@pytest.mark.parametrize("count", [0, 51])
def test_batch_size_cannot_exceed_authorized_shape(batch_cases, count):
    cases = [] if count == 0 else batch_cases + [copy.deepcopy(batch_cases[0])]
    with pytest.raises(BatchEvaluationError, match="batch_case_count_invalid"):
        BatchEvaluator(cases, good_invoke)


@pytest.mark.parametrize("duplicate", ["case_id", "input_hash"])
def test_duplicate_case_identity_or_payload_is_rejected(batch_cases, duplicate):
    cases = copy.deepcopy(batch_cases[:2])
    if duplicate == "case_id":
        cases[1]["case_id"] = cases[0]["case_id"]
    else:
        cases[1]["payload"] = copy.deepcopy(cases[0]["payload"])
    with pytest.raises(BatchEvaluationError, match="batch_cases_not_unique"):
        BatchEvaluator(cases, good_invoke)


@pytest.mark.parametrize("mutation", ["real", "invalid_hash", "extra_case_field", "unsafe_label"])
def test_all_inputs_are_rejected_before_any_invocation(batch_cases, mutation):
    cases = copy.deepcopy(batch_cases[:2])
    if mutation == "real":
        cases[1]["payload"]["synthetic"] = False
    elif mutation == "invalid_hash":
        cases[1]["payload"]["input_hash"] = "0" * 64
    elif mutation == "extra_case_field":
        cases[1]["secret"] = "SYNTHETIC_SECRET"
    else:
        cases[1]["scenario"] = "SYNTHETIC_SECRET\nINJECT"
    called = []
    with pytest.raises(BatchEvaluationError) as caught:
        BatchEvaluator(cases, lambda *args: called.append(args))
    assert not called and "SYNTHETIC_SECRET" not in str(caught.value)


def test_inputs_requests_and_template_are_frozen_before_live_work(batch_cases, monkeypatch):
    import model_batch_runner as runner

    cases = copy.deepcopy(batch_cases[:2])
    observed = []

    def invoke(case, request):
        observed.append(request.messages[0].content)
        result = result_for(case)
        request.messages.clear()
        request.output_schema.clear()
        case["payload"].clear()
        return result

    evaluator = BatchEvaluator(cases, invoke)
    cases[0]["payload"].clear()
    monkeypatch.setattr(runner, "_request", lambda *args, **kwargs: pytest.fail("template reread"))
    assert evaluator.run_case(0)["business_pass"] is True
    assert evaluator.run_case(1)["business_pass"] is True
    assert len(set(observed)) == 1


def test_data_dependent_template_is_not_an_equivalent_batch(batch_cases, monkeypatch):
    import model_batch_runner as runner

    original = runner._request

    def changed(payload, **kwargs):
        from sleepscape.models.base import ChatMessage

        request = original(payload, **kwargs)
        request.messages[2] = ChatMessage("user", payload["input_hash"])
        return request

    monkeypatch.setattr(runner, "_request", changed)
    with pytest.raises(BatchEvaluationError, match="batch_request_template_changed"):
        BatchEvaluator(batch_cases[:2], good_invoke)


def test_schema_pass_is_separate_from_evidence_business_failure(batch_cases):
    def invoke(case, _request):
        wire = wire_for(case)
        wire["summary"]["evidence_ids"] = ["F999"]
        return ModelResult(json.dumps(wire), "qwen3.8-max", "stop", {"total_tokens": 25})

    row = BatchEvaluator(batch_cases[:1], invoke).run_case(0)
    assert row["json_pass"] is row["schema_pass"] is True
    assert row["business_pass"] is False
    assert row["outcome"] == "business_failed" and row["error_code"] == "output_business_mismatch"


def test_model_result_is_rechecked_before_schema_success(batch_cases):
    def invoke(case, _request):
        wire = wire_for(case)
        del wire["observations"]
        wire["summary"]["text"] = "SYNTHETIC_PRIVATE_RESPONSE"
        return ModelResult(json.dumps(wire), "qwen3.8-max", "stop")

    row = BatchEvaluator(batch_cases[:1], invoke).run_case(0)
    assert row["json_pass"] is True and row["schema_pass"] is False
    assert row["business_pass"] is None and row["error_code"] == "output_schema_required"
    assert "SYNTHETIC_PRIVATE_RESPONSE" not in json.dumps(row)


@pytest.mark.parametrize(
    "code,outcome,json_pass,schema_pass",
    [
        ("output_schema_max_items", "schema_failed", True, False),
        ("output_type_report_array_multiple", "schema_failed", True, False),
        ("invalid_json", "protocol_failed", False, None),
        ("network_error", "transport_failed", None, None),
        ("rate_limited", "transport_failed", None, None),
        ("model_mismatch", "protocol_failed", None, None),
        ("output_truncated", "protocol_failed", None, None),
    ],
)
def test_adapter_failure_stages_do_not_claim_unobserved_schema(
    batch_cases, code, outcome, json_pass, schema_pass
):
    def invoke(*args):
        raise ModelError(code)

    row = BatchEvaluator(batch_cases[:1], invoke).run_case(0)
    assert row["outcome"] == outcome
    assert row["json_pass"] is json_pass and row["schema_pass"] is schema_pass
    assert row["business_pass"] is None and row["usage"] == {}


def test_invalid_json_from_stub_never_leaks_body(batch_cases):
    result = ModelResult('{"SYNTHETIC_PRIVATE_RESPONSE":', "qwen3.8-max", "stop")
    row = BatchEvaluator(batch_cases[:1], lambda *args: result).run_case(0)
    assert row["error_code"] == "invalid_json" and row["json_pass"] is False
    assert "SYNTHETIC_PRIVATE_RESPONSE" not in json.dumps(row)


def test_unknown_callback_exception_is_sanitized_and_cached(batch_cases):
    count = 0

    def invoke(*args):
        nonlocal count
        count += 1
        raise ValueError("SYNTHETIC_PRIVATE_CALLBACK_TEXT")

    evaluator = BatchEvaluator(batch_cases[:1], invoke)
    row = evaluator.run_case(0)
    assert evaluator.run_case(0) == row and count == 1
    assert row["error_code"] == "model_operation_failed"
    assert "SYNTHETIC_PRIVATE_CALLBACK_TEXT" not in json.dumps(evaluator.summary())


def test_interruption_is_saved_before_propagating_and_not_repeated(batch_cases):
    saved, called = [], []

    def invoke(*args):
        called.append(True)
        raise KeyboardInterrupt("SYNTHETIC_PRIVATE_INTERRUPT")

    evaluator = BatchEvaluator(batch_cases[:1], invoke, sink=saved.append)
    with pytest.raises(KeyboardInterrupt) as caught:
        evaluator.run_case(0)
    assert str(caught.value) == "" and len(saved) == 1
    assert evaluator.run_case(0)["error_code"] == "model_worker_failed" and len(called) == 1


def test_sink_failure_cannot_repeat_paid_call(batch_cases):
    called = []

    def invoke(case, request):
        called.append(True)
        return result_for(case)

    def sink(_row):
        raise ValueError("SYNTHETIC_PRIVATE_SINK")

    evaluator = BatchEvaluator(batch_cases[:1], invoke, sink=sink)
    with pytest.raises(BatchEvaluationError, match="^batch_sink_failed$"):
        evaluator.run_case(0)
    assert evaluator.run_case(0)["business_pass"] is True and len(called) == 1


def test_resume_reuses_metadata_without_any_invocation(batch_cases):
    original = BatchEvaluator(batch_cases[:2], good_invoke)
    original.run_case(0)
    copied = original.rows
    called = []

    def invoke(case, request):
        called.append(case["case_id"])
        return result_for(case)

    resumed = BatchEvaluator(batch_cases[:2], invoke, existing_results=copied)
    assert resumed.run_case(0) == copied[0] and not called
    resumed.run_case(1)
    assert called == [batch_cases[1]["case_id"]]
    copied[0]["usage"].clear()
    assert resumed.rows[0]["usage"]


@pytest.mark.parametrize("change", ["extra", "hash", "template", "success_flags", "error"])
def test_resume_rejects_foreign_or_forged_safe_rows(batch_cases, change):
    original = BatchEvaluator(batch_cases[:1], good_invoke)
    row = original.run_case(0)
    if change == "extra":
        row["response"] = "SYNTHETIC_PRIVATE_RESUME"
    elif change == "hash":
        row["input_hash"] = "0" * 64
    elif change == "template":
        row["template_sha256"] = "0" * 64
    elif change == "success_flags":
        row["schema_pass"] = False
    else:
        row["error_code"] = "SYNTHETIC_PRIVATE_RESUME"
    with pytest.raises(BatchEvaluationError, match="^batch_resume_invalid$"):
        BatchEvaluator(batch_cases[:1], good_invoke, existing_results=[row])


def test_summary_counts_unknown_failures_and_only_known_usage(batch_cases):
    def invoke(case, _request):
        if case["case_id"] == batch_cases[0]["case_id"]:
            return result_for(case)
        raise ModelError("network_error")

    evaluator = BatchEvaluator(batch_cases[:3], invoke)
    evaluator.run_case(0)
    evaluator.run_case(1)
    result = evaluator.summary()
    assert result["requested_cases"] == 3 and result["evaluated_cases"] == 2
    assert result["remaining_cases"] == 1
    assert result["schema_pass_count"] == result["business_pass_count"] == 1
    assert result["schema_unknown_count"] == 1
    assert result["known_usage"]["total_tokens"] == 120
    assert result["usage_excludes_unknown_failures"] is True
    assert result["schema_pass_rate_of_requested"] == 1 / 3


def test_normal_pytest_live_entry_skips_without_authorized_evaluator(monkeypatch):
    from test_live_model_batch import test_live_model_batch_case

    from sleepscape.models import openai_compatible

    monkeypatch.setattr(
        openai_compatible, "build_opener", lambda *args: pytest.fail("unexpected live network")
    )
    with pytest.raises(pytest.skip.Exception):
        test_live_model_batch_case(SimpleNamespace(config=SimpleNamespace()), 0)


def test_safe_rows_never_contain_input_prompt_response_or_extra_usage(batch_cases):
    def invoke(case, _request):
        result = result_for(case)
        result.usage["SYNTHETIC_PRIVATE_USAGE"] = 88
        return result

    row = BatchEvaluator(batch_cases[:1], invoke).run_case(0)
    assert not ({"payload", "response", "text", "prompt", "endpoint", "key"} & set(row))
    assert "SYNTHETIC_PRIVATE_USAGE" not in json.dumps(row)
