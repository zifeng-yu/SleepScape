"""Explicit provider projection keeps the complete original local contract."""

import copy
import io
import json
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError

import pytest
import test_models as base_models
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from test_models import (  # Shared isolated fake transport; never makes a live HTTP request.
    answer,
    request,
    transport,
)

from sleepscape.models import ChatMessage, ModelError
from sleepscape.models import openai_compatible as adapter
from sleepscape.models.audit import error_metadata
from sleepscape.models.base import OUTPUT_SCHEMA_ERRORS, OUTPUT_TYPE_ERRORS
from sleepscape.models.schema_transport import transport_schema
from sleepscape.models.settings import (
    configuration_fingerprint,
    configuration_status,
    load_model_settings,
)
from sleepscape.resources import contract_schema, validate_sleep_output
from sleepscape.services.model_health import _request as capability_request


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    base_models.isolate_environment_and_network.__wrapped__(monkeypatch)


@pytest.fixture
def settings(tmp_path):
    return base_models.settings.__wrapped__(tmp_path)


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["values", "contains"],
    "properties": {
        "values": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
            "contains": {"const": "required"},
            "minContains": 1,
            "maxContains": 1,
        },
        "contains": {"type": "string"},
    },
}


def test_explicit_profile_changes_only_sent_schema(settings):
    model = load_model_settings(settings)
    assert model.schema_profile == "aliyun-compatible-1"
    source = copy.deepcopy(SCHEMA)
    wire, local = adapter._payload(model, request(output_schema=source))
    value = json.loads(wire)
    assert value["response_format"]["type"] == "json_schema"
    assert value["response_format"]["json_schema"]["strict"] is True
    assert local is source and source == SCHEMA
    projected = value["response_format"]["json_schema"]["schema"]
    assert projected["properties"]["values"] == {"type": "array", "items": {"type": "string"}}
    assert projected["properties"]["contains"] == {"type": "string"}
    assert projected["required"] == ["values", "contains"]


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize(
    "items,code",
    [
        (["required", "required"], "output_schema_unique_items"),
        (["other"], "output_schema_contains"),
    ],
)
def test_provider_allowed_duplicate_or_missing_contains_is_rejected_locally(
    settings, monkeypatch, items, code, mode
):
    result = {"values": items, "contains": "field name preserved"}
    projected = transport_schema(SCHEMA, "aliyun-compatible-1")
    Draft202012Validator(projected).validate(result)
    payload = answer(
        choices=[{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]
    )
    calls, _ = transport(monkeypatch, payload)
    with pytest.raises(ModelError, match=code):
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(request(output_schema=SCHEMA))
    assert len(calls) == 1


def test_original_valid_output_still_passes(settings, monkeypatch):
    result = {"values": ["required", "other"], "contains": "kept"}
    calls, _ = transport(
        monkeypatch,
        answer(
            choices=[
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(result)},
                }
            ]
        ),
    )
    received = adapter.OpenAICompatibleClient(load_model_settings(settings)).complete(
        request(output_schema=SCHEMA)
    )
    assert json.loads(received.text) == result and len(calls) == 1


def test_schema_context_traversal_preserves_property_definition_and_literal_names():
    literal = {"contains": {"uniqueItems": True, "multipleOf": 2}, "propertyNames": "data"}
    value = {
        "type": "object",
        "properties": {
            "contains": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "number", "multipleOf": 2},
            },
            "literal": {
                "const": literal,
                "default": literal,
                "examples": [literal],
                "enum": [literal],
            },
            "reference": {"$ref": "#/$defs/contains"},
        },
        "$defs": {
            "contains": {"type": "array", "contains": {"type": "number"}, "additionalItems": False}
        },
        "propertyNames": {"pattern": "^[a-z]+$"},
        "patternProperties": {"^x": {"type": "string"}},
        "allOf": [
            {
                "if": {"properties": {"contains": {"minContains": 1}}},
                "then": {"properties": {"contains": {"maxContains": 2}}},
            }
        ],
    }
    original = copy.deepcopy(value)
    projected = transport_schema(value, "aliyun-compatible-1")
    assert value == original
    assert projected["properties"]["contains"] == {"type": "array", "items": {"type": "number"}}
    assert projected["properties"]["literal"] == value["properties"]["literal"]
    assert projected["$defs"]["contains"] == {"type": "array"}
    assert projected["allOf"] == [
        {"if": {"properties": {"contains": {}}}, "then": {"properties": {"contains": {}}}}
    ]
    assert "propertyNames" not in projected and "patternProperties" not in projected
    projected["properties"]["literal"]["const"]["contains"]["uniqueItems"] = False
    assert value == original


def test_projection_handles_other_schema_locations_and_leaves_dependencies_names():
    array = {"type": "array", "uniqueItems": True}
    value = {
        "type": "object",
        "dependentSchemas": {"contains": array},
        "dependencies": {"contains": array, "uniqueItems": ["contains"]},
        "definitions": {"contains": array},
        "properties": {"tuple": {"prefixItems": [array], "items": False}},
        "additionalProperties": array,
        "unevaluatedProperties": False,
        "not": {"contains": {"type": "integer"}},
        "anyOf": [array],
        "oneOf": [array],
    }
    result = transport_schema(value, "aliyun-compatible-1")
    assert result["dependentSchemas"]["contains"] == {"type": "array"}
    assert result["dependencies"]["contains"] == {"type": "array"}
    assert result["dependencies"]["uniqueItems"] == ["contains"]
    assert result["properties"]["tuple"] == {"prefixItems": [{"type": "array"}], "items": False}
    assert result["not"] == {} and result["anyOf"] == result["oneOf"] == [{"type": "array"}]


def test_removed_reference_target_fails_before_network(settings):
    value = {
        "type": "object",
        "additionalProperties": False,
        "required": ["values"],
        "properties": {"values": {"$ref": "#/propertyNames"}},
        "propertyNames": {"type": "string"},
    }
    with pytest.raises(ModelError, match="capability_unavailable"):
        adapter._payload(load_model_settings(settings), request(output_schema=value))


def test_native_profile_is_unchanged_and_source_not_aliased(settings):
    model = replace(load_model_settings(settings), schema_profile="native")
    wire, original = adapter._payload(model, request(output_schema=SCHEMA))
    assert json.loads(wire)["response_format"]["json_schema"]["schema"] == SCHEMA
    assert original is SCHEMA
    projected = transport_schema(SCHEMA, "native")
    projected["properties"]["values"]["uniqueItems"] = False
    assert SCHEMA["properties"]["values"]["uniqueItems"] is True


def test_missing_profile_defaults_native_and_fingerprint_changes(settings):
    previous = configuration_fingerprint(settings)
    text = settings.llm_config_file.read_text()
    settings.llm_config_file.write_text(
        text.replace('schema_profile = "aliyun-compatible-1"\n', "")
    )
    assert load_model_settings(settings).schema_profile == "native"
    assert configuration_fingerprint(settings) != previous
    settings.llm_config_file.write_text(text)
    assert configuration_fingerprint(settings) == previous


@pytest.mark.parametrize("profile", ["unknown-profile", "", None, True, []])
def test_unknown_direct_profile_fails_closed(settings, profile):
    with pytest.raises(ModelError, match="model_configuration_invalid"):
        adapter._payload(replace(load_model_settings(settings), schema_profile=profile), request())


@pytest.mark.parametrize("toml_value", ['"unknown-profile"', '""', "true", "1", "[]"])
def test_unknown_config_profile_is_invalid(settings, toml_value):
    text = settings.llm_config_file.read_text()
    settings.llm_config_file.write_text(text.replace('"aliyun-compatible-1"', toml_value))
    assert configuration_status(settings)["configuration_state"] == "invalid_configuration"
    with pytest.raises(ModelError, match="model_configuration_invalid"):
        load_model_settings(settings)


def test_real_report_schema_is_projected_without_replacing_local_contract(settings):
    source = contract_schema("output")
    previous = copy.deepcopy(source)
    wire, local = adapter._payload(load_model_settings(settings), request(output_schema=source))
    projected = json.loads(wire)["response_format"]["json_schema"]["schema"]
    assert source == previous and local is source
    assert "uniqueItems" in source["properties"]["limitations"]
    assert "uniqueItems" not in projected["properties"]["limitations"]
    assert "contains" in source["allOf"][0]["then"]["properties"]["limitations"]
    assert projected["allOf"][0]["then"]["properties"]["limitations"] == {}
    Draft202012Validator.check_schema(projected)


@pytest.fixture
def report_output():
    value = json.loads(Path("docs/contracts/sleep-analysis.example.json").read_text())["output"]
    value["hypotheses"] = [
        {
            "factor": "sound",
            "text": "可继续观察声音变化，不能据此确定原因。",
            "evidence_ids": ["F010"],
            "evidence_level": "limited",
            "alternatives": ["生活因素仍然未知。"],
        }
    ]
    Draft202012Validator(contract_schema("output")).validate(value)
    return value


_PRIVATE_DIAGNOSTIC = "SYNTHETIC_DIAGNOSTIC_PRIVATE_VALUE"


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize(
    "path,replacement,code",
    [
        ((), _PRIVATE_DIAGNOSTIC, "output_type_report_string"),
        ((), [_PRIVATE_DIAGNOSTIC], "output_type_report_array_single_nonobject"),
        ((), [], "output_type_report_array_empty"),
        ((), [_PRIVATE_DIAGNOSTIC, _PRIVATE_DIAGNOSTIC], "output_type_report_array_multiple"),
        ((), None, "output_type_report_null"),
        ((), True, "output_type_report_scalar"),
        ((), 42, "output_type_report_scalar"),
        (("summary",), _PRIVATE_DIAGNOSTIC, "output_type_summary"),
        (("summary", "text"), {_PRIVATE_DIAGNOSTIC: None}, "output_type_text"),
        (("summary", "evidence_ids"), _PRIVATE_DIAGNOSTIC, "output_type_evidence_ids"),
        (("summary", "evidence_ids", 0), {_PRIVATE_DIAGNOSTIC: None}, "output_type_evidence_ids"),
        (("observations",), _PRIVATE_DIAGNOSTIC, "output_type_observations"),
        (("observations", 0), _PRIVATE_DIAGNOSTIC, "output_type_observation_item"),
        (("observations", 3), _PRIVATE_DIAGNOSTIC, "output_type_observation_item"),
        (("observations", 0, "text"), {_PRIVATE_DIAGNOSTIC: None}, "output_type_text"),
        (("observations", 0, "evidence_ids"), _PRIVATE_DIAGNOSTIC, "output_type_evidence_ids"),
        (
            ("observations", 0, "evidence_ids", 0),
            {_PRIVATE_DIAGNOSTIC: None},
            "output_type_evidence_ids",
        ),
        (("hypotheses",), _PRIVATE_DIAGNOSTIC, "output_type_hypotheses"),
        (("hypotheses", 0), _PRIVATE_DIAGNOSTIC, "output_type_hypothesis_item"),
        (("hypotheses", 0, "text"), {_PRIVATE_DIAGNOSTIC: None}, "output_type_text"),
        (("hypotheses", 0, "evidence_ids"), _PRIVATE_DIAGNOSTIC, "output_type_evidence_ids"),
        (
            ("hypotheses", 0, "evidence_ids", 0),
            {_PRIVATE_DIAGNOSTIC: None},
            "output_type_evidence_ids",
        ),
        (("hypotheses", 0, "alternatives"), _PRIVATE_DIAGNOSTIC, "output_type_alternatives"),
        (
            ("hypotheses", 0, "alternatives", 0),
            {_PRIVATE_DIAGNOSTIC: None},
            "output_type_alternatives",
        ),
        (("actions",), _PRIVATE_DIAGNOSTIC, "output_type_actions"),
        (("actions", 0), _PRIVATE_DIAGNOSTIC, "output_type_action_item"),
        (("actions", 0, "text"), {_PRIVATE_DIAGNOSTIC: None}, "output_type_text"),
        (("actions", 0, "observe"), {_PRIVATE_DIAGNOSTIC: None}, "output_type_observe"),
        (("actions", 0, "evidence_ids"), _PRIVATE_DIAGNOSTIC, "output_type_evidence_ids"),
        (
            ("actions", 0, "evidence_ids", 0),
            {_PRIVATE_DIAGNOSTIC: None},
            "output_type_evidence_ids",
        ),
        (("limitations",), _PRIVATE_DIAGNOSTIC, "output_type_limitations"),
        (("limitations", 0), {_PRIVATE_DIAGNOSTIC: None}, "output_type_limitations"),
        (("night_date",), {_PRIVATE_DIAGNOSTIC: None}, "output_type_identity"),
        (("input_hash",), {_PRIVATE_DIAGNOSTIC: None}, "output_type_identity"),
    ],
)
def test_canonical_report_type_diagnostics_are_fixed_and_private(
    settings, monkeypatch, report_output, path, replacement, code, mode
):
    value = copy.deepcopy(report_output)
    if path:
        target = value
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = replacement
    else:
        value = replacement
    with pytest.raises(ValidationError) as direct:
        Draft202012Validator(contract_schema("output")).validate(value)
    assert direct.value.validator == "type"
    assert tuple(direct.value.absolute_path) == path
    calls, _ = transport(
        monkeypatch,
        answer(choices=[{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]),
    )
    with pytest.raises(ModelError) as caught:
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(
            request(output_schema=contract_schema("output"), schema_name="sleepscape_sleep_report")
        )
    assert len(calls) == 1
    assert caught.value.code == str(caught.value) == code
    assert code in OUTPUT_TYPE_ERRORS <= OUTPUT_SCHEMA_ERRORS
    assert _PRIVATE_DIAGNOSTIC not in json.dumps(error_metadata(caught.value))
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


def test_report_type_diagnostics_do_not_apply_to_other_model_requests(
    settings, monkeypatch, report_output
):
    report_output["summary"] = _PRIVATE_DIAGNOSTIC
    transport(
        monkeypatch,
        answer(
            choices=[{"finish_reason": "stop", "message": {"content": json.dumps(report_output)}}]
        ),
    )
    with pytest.raises(ModelError) as caught:
        adapter.OpenAICompatibleClient(load_model_settings(settings)).complete(
            request(output_schema=contract_schema("output"), schema_name="different_request")
        )
    assert caught.value.code == "output_schema_type"


@pytest.mark.parametrize(
    "path",
    [
        [_PRIVATE_DIAGNOSTIC],
        ["summary", _PRIVATE_DIAGNOSTIC],
        ["observations", "0", "text"],
        ["actions", -1, "text"],
        ["summary", "evidence_ids", True],
        ["summary", "evidence_ids", 0, _PRIVATE_DIAGNOSTIC],
        ["observations", 0, "alternatives"],
        ["hypotheses", 0, "observe"],
        ["actions", 0, "evidence_ids", 0, _PRIVATE_DIAGNOSTIC],
    ],
)
def test_unknown_report_type_paths_stay_generic_without_disclosing_path(
    settings, monkeypatch, path
):
    transport(monkeypatch)

    def invalid(*args, **kwargs):
        raise ValidationError(
            _PRIVATE_DIAGNOSTIC, validator="type", path=path, instance=_PRIVATE_DIAGNOSTIC
        )

    monkeypatch.setattr(adapter.Draft202012Validator, "validate", invalid)
    with pytest.raises(ModelError) as caught:
        adapter.OpenAICompatibleClient(load_model_settings(settings)).complete(
            request(schema_name="sleepscape_sleep_report")
        )
    assert caught.value.code == str(caught.value) == "output_schema_type"
    assert _PRIVATE_DIAGNOSTIC not in json.dumps(error_metadata(caught.value))


def test_known_report_path_does_not_override_non_type_validation(settings, monkeypatch):
    transport(monkeypatch)

    def invalid(*args, **kwargs):
        raise ValidationError(_PRIVATE_DIAGNOSTIC, validator="required", path=["summary"])

    monkeypatch.setattr(adapter.Draft202012Validator, "validate", invalid)
    with pytest.raises(ModelError) as caught:
        adapter.OpenAICompatibleClient(load_model_settings(settings)).complete(
            request(schema_name="sleepscape_sleep_report")
        )
    assert caught.value.code == "output_schema_required"


def test_explicit_json_object_sends_only_mode_and_preserves_complete_local_schema(
    settings, monkeypatch
):
    model = replace(load_model_settings(settings), response_mode="json_object")
    source = copy.deepcopy(SCHEMA)
    wire, local = adapter._payload(model, request(output_schema=source))
    assert json.loads(wire)["response_format"] == {"type": "json_object"}
    assert local is source and source == SCHEMA
    # There is no provider schema to project; unsupported transport constraints
    # remain in the local validator rather than being silently discarded.
    monkeypatch.setattr(
        adapter, "transport_schema", lambda *_: pytest.fail("Unexpected projection")
    )
    calls, _ = transport(monkeypatch)
    assert adapter.OpenAICompatibleClient(model).complete(request()).finish_reason == "stop"
    assert len(calls) == 1
    assert json.loads(calls[0][0].data)["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
def test_each_explicit_mode_rejects_missing_required_fields(settings, monkeypatch, mode):
    transport(
        monkeypatch,
        answer(choices=[{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]),
    )
    with pytest.raises(ModelError, match="^output_schema_required$"):
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(request())


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
def test_each_explicit_mode_rejects_invalid_local_schema_before_network(settings, mode):
    with pytest.raises(ModelError, match="^invalid_model_request$"):
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(request(output_schema={"type": "string"}))


def test_json_object_requires_json_hint_in_system_or_user_before_network(settings):
    model = replace(load_model_settings(settings), response_mode="json_object")
    with pytest.raises(ModelError, match="^invalid_model_request$"):
        adapter.OpenAICompatibleClient(model).complete(
            request(
                messages=[
                    ChatMessage("system", "Return a structured object."),
                    ChatMessage("assistant", "JSON"),
                ]
            )
        )
    wire, _ = adapter._payload(model, request(messages=[ChatMessage("user", "Return json only.")]))
    assert json.loads(wire)["response_format"] == {"type": "json_object"}


def test_synthetic_capability_request_is_explicit_for_json_object(settings):
    synthetic = capability_request("a" * 32)
    assert "JSON" in synthetic.messages[0].content
    assert "Return exactly this JSON object" in synthetic.messages[1].content
    wire, local = adapter._payload(
        replace(load_model_settings(settings), response_mode="json_object"), synthetic
    )
    assert json.loads(wire)["response_format"] == {"type": "json_object"}
    assert local is synthetic.output_schema
    assert set(local["required"]) == {"ok", "nonce", "items", "note"}


def test_response_mode_default_compatibility_explicit_deployment_and_fingerprint(settings):
    original = settings.llm_config_file.read_text()
    assert load_model_settings(settings).response_mode == "json_schema"
    before = configuration_fingerprint(settings)
    settings.llm_config_file.write_text(original.replace('response_mode = "json_schema"\n', ""))
    assert load_model_settings(settings).response_mode == "json_schema"
    assert configuration_fingerprint(settings) == before
    settings.llm_config_file.write_text(Path("configs/models.toml").read_text())
    assert load_model_settings(settings).response_mode == "json_object"
    assert configuration_fingerprint(settings) != before
    settings.llm_config_file.write_text(original)
    assert configuration_fingerprint(settings) == before


@pytest.mark.parametrize("mode", ["unknown", "", None, True, []])
def test_unknown_direct_response_modes_fail_closed(settings, mode):
    with pytest.raises(ModelError, match="^model_configuration_invalid$"):
        adapter._payload(replace(load_model_settings(settings), response_mode=mode), request())


@pytest.mark.parametrize("toml_value", ['"unknown"', '""', "true", "1", "[]"])
def test_unknown_config_response_modes_fail_closed(settings, toml_value):
    text = settings.llm_config_file.read_text()
    settings.llm_config_file.write_text(
        text.replace('response_mode = "json_schema"', f"response_mode = {toml_value}")
    )
    assert configuration_status(settings)["configuration_state"] == "invalid_configuration"
    with pytest.raises(ModelError, match="^model_configuration_invalid$"):
        load_model_settings(settings)


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
def test_provider_failure_never_switches_response_mode(settings, monkeypatch, mode):
    error = HTTPError(
        base_models.BASE,
        400,
        "Bad request",
        {},
        io.BytesIO(b'{"error":{"message":"response_format unsupported"}}'),
    )
    calls, _ = transport(monkeypatch, error=error)
    with pytest.raises(ModelError, match="^capability_unavailable$"):
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(request())
    assert len(calls) == 1
    assert json.loads(calls[0][0].data)["response_format"]["type"] == mode


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize("normalization", ["json_string_envelope", "single_object_array"])
def test_single_transport_envelope_normalizes_only_a_valid_object(
    settings, monkeypatch, mode, normalization
):
    expected = {"ok": True, "note": "synthetic"}
    content = json.dumps(
        json.dumps(expected) if normalization == "json_string_envelope" else [expected]
    )
    transport(
        monkeypatch,
        answer(choices=[{"finish_reason": "stop", "message": {"content": content}}]),
    )
    result = adapter.OpenAICompatibleClient(
        replace(load_model_settings(settings), response_mode=mode)
    ).complete(request())
    assert json.loads(result.text) == expected
    assert result.transport_normalization == normalization
    assert result.usage == {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13}


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize("normalization", ["json_string_envelope", "single_object_array"])
@pytest.mark.parametrize(
    "inner,code",
    [
        ('{"ok":true}', "output_schema_required"),
        ('{"ok":true,"note":null}', "output_schema_type"),
        ('{"ok":true,"note":"synthetic","extra":"x"}', "output_schema_additional_properties"),
    ],
)
def test_normalized_envelope_still_obeys_complete_original_schema(
    settings, monkeypatch, mode, inner, code, normalization
):
    content = json.dumps(inner if normalization == "json_string_envelope" else [json.loads(inner)])
    transport(
        monkeypatch,
        answer(choices=[{"finish_reason": "stop", "message": {"content": content}}]),
    )
    with pytest.raises(ModelError) as caught:
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(request())
    assert caught.value.code == code


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize(
    "inner",
    [
        json.dumps('{"ok":true,"note":"synthetic"}'),
        '[{"ok":true,"note":"synthetic"}]',
        "null",
        "true",
        "42",
        '{"ok":true,"ok":false,"note":"synthetic"}',
        '{"ok":true,"note":NaN}',
        '{"ok":true,"note":1e999}',
        '```json\n{"ok":true,"note":"synthetic"}\n```',
        "not JSON",
    ],
)
def test_non_object_invalid_or_double_envelopes_are_not_repaired(
    settings, monkeypatch, mode, inner
):
    transport(
        monkeypatch,
        answer(choices=[{"finish_reason": "stop", "message": {"content": json.dumps(inner)}}]),
    )
    with pytest.raises(ModelError) as caught:
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(request(schema_name="sleepscape_sleep_report"))
    assert caught.value.code == "output_type_report_string"


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize("private", [base_models.PRIVATE, "models.example.test"])
def test_inner_envelope_private_literals_are_rechecked_after_decode(
    settings, monkeypatch, mode, private
):
    inner = json.dumps({"ok": True, "note": private})
    # Hide a character in the inner JSON string; checking only the outer text
    # would miss the forbidden complete literal.
    inner = inner.replace(private, f"\\u{ord(private[0]):04x}" + private[1:])
    transport(
        monkeypatch,
        answer(choices=[{"finish_reason": "stop", "message": {"content": json.dumps(inner)}}]),
    )
    with pytest.raises(ModelError) as caught:
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(request())
    assert caught.value.code == "invalid_response"
    assert private not in str(caught.value) + json.dumps(error_metadata(caught.value))


def test_unwrapped_object_retains_default_normalization(settings, monkeypatch):
    transport(monkeypatch)
    result = adapter.OpenAICompatibleClient(load_model_settings(settings)).complete(request())
    assert result.transport_normalization == "none"


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize(
    "value,code",
    [
        ([], "output_type_report_array_empty"),
        (
            [{"ok": True, "note": "synthetic"}, {"ok": True, "note": "synthetic"}],
            "output_type_report_array_multiple",
        ),
        ([{"ok": True, "note": "synthetic"}, None], "output_type_report_array_multiple"),
        ([[{"ok": True, "note": "synthetic"}]], "output_type_report_array_single_nonobject"),
        (
            [json.dumps({"ok": True, "note": "synthetic"})],
            "output_type_report_array_single_nonobject",
        ),
        ([None], "output_type_report_array_single_nonobject"),
        ([42], "output_type_report_array_single_nonobject"),
        ([True], "output_type_report_array_single_nonobject"),
    ],
)
def test_array_normalization_never_selects_merges_or_recursively_decodes(
    settings, monkeypatch, mode, value, code
):
    transport(
        monkeypatch,
        answer(choices=[{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]),
    )
    with pytest.raises(ModelError) as caught:
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(request(schema_name="sleepscape_sleep_report"))
    assert caught.value.code == code


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize(
    "text", ['[{"ok":true,"ok":false,"note":"synthetic"}]', '[{"ok":true,"note":NaN}]']
)
def test_array_normalization_never_bypasses_strict_json(settings, monkeypatch, mode, text):
    transport(
        monkeypatch, answer(choices=[{"finish_reason": "stop", "message": {"content": text}}])
    )
    with pytest.raises(ModelError, match="^invalid_json$"):
        adapter.OpenAICompatibleClient(
            replace(load_model_settings(settings), response_mode=mode)
        ).complete(request())


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize("normalization", ["json_string_envelope", "single_object_array"])
def test_both_single_envelopes_preserve_canonical_report_and_business_validation(
    settings, monkeypatch, mode, normalization
):
    example = json.loads(Path("docs/contracts/sleep-analysis.example.json").read_text())
    expected = example["output"]
    content = json.dumps(
        json.dumps(expected) if normalization == "json_string_envelope" else [expected]
    )
    transport(
        monkeypatch, answer(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    )
    result = adapter.OpenAICompatibleClient(
        replace(load_model_settings(settings), response_mode=mode)
    ).complete(
        request(output_schema=contract_schema("output"), schema_name="sleepscape_sleep_report")
    )
    output = json.loads(result.text)
    assert output == expected and result.transport_normalization == normalization
    validate_sleep_output(output, example["input"])
