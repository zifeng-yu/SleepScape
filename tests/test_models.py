"""Synthetic model configurations and protocol failures; never use live keys or HTTP."""

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from jsonschema.exceptions import ValidationError

from sleepscape.config import Settings
from sleepscape.models import ChatMessage, ModelError, ModelRequest, ModelResult
from sleepscape.models import openai_compatible as adapter
from sleepscape.models.audit import error_metadata, result_metadata, safe_usage
from sleepscape.models.base import ERROR_CODES, OUTPUT_SCHEMA_ERRORS
from sleepscape.models.factory import create_client
from sleepscape.models.settings import (
    configuration_fingerprint,
    configuration_status,
    load_model_settings,
    read_api_key,
)

PRIVATE = "SYNTHETIC_MODEL_PRIVATE_VALUE"
BASE = "https://models.example.test/compatible-mode/v1"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok", "note"],
    "properties": {"ok": {"const": True}, "note": {"type": "string", "maxLength": 80}},
}


@pytest.fixture(autouse=True)
def isolate_environment_and_network(monkeypatch):
    import os

    for name in os.environ:
        if name.startswith("SLEEPSCAPE_LLM_"):
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        pytest.fail("Model tests must not use a live network")

    monkeypatch.setattr(adapter, "build_opener", forbidden)


@pytest.fixture
def settings(tmp_path):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    key = folder / "key"
    key.write_text(PRIVATE)
    key.chmod(0o600)
    config = tmp_path / "models.toml"
    # Exercise the strict protocol independently of the deployed explicit mode.
    config.write_text(
        Path("configs/models.toml")
        .read_text()
        .replace('response_mode = "json_object"', 'response_mode = "json_schema"')
    )
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        llm_enabled=True,
        llm_base_url=BASE,
        llm_key_file=key,
        llm_config_file=config,
    )


def request(**changes):
    return ModelRequest(
        **{
            "messages": [
                ChatMessage("system", "Return JSON only."),
                ChatMessage("user", "Synthetic check"),
            ],
            "output_schema": SCHEMA,
            **changes,
        }
    )


def answer(**changes):
    return {
        "model": "qwen3.8-max",
        "choices": [
            {"finish_reason": "stop", "message": {"content": '{"ok":true,"note":"synthetic"}'}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
        **changes,
    }


class Response(io.BytesIO):
    def __init__(self, raw, headers=None, url=BASE + "/chat/completions", status=200):
        super().__init__(raw)
        self.headers = headers or {}
        self.url = url
        self.status = status

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status


def transport(monkeypatch, value=None, *, raw=None, error=None, headers=None, url=None, status=200):
    calls = []
    response = Response(
        raw if raw is not None else json.dumps(answer() if value is None else value).encode(),
        headers=headers,
        url=url or BASE + "/chat/completions",
        status=status,
    )

    def open_(call, *, timeout):
        calls.append((call, timeout))
        if error:
            raise error
        return response

    def builder(handler):
        assert handler is adapter.NoRedirect
        return SimpleNamespace(open=open_)

    monkeypatch.setattr(adapter, "build_opener", builder)
    return calls, response


def test_configuration_status_is_offline_and_does_not_read_key(settings, monkeypatch):
    real_read_bytes = Path.read_bytes

    def guarded(path):
        assert path != settings.llm_key_file
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    result = configuration_status(settings)
    assert result == {
        "configured": True,
        "enabled": True,
        "model_ref": "sleep_analyst",
        "model": "qwen3.8-max",
        "provider": "openai_compatible",
        "configuration_state": "configured",
        "code": None,
    }
    assert BASE not in json.dumps(result)
    assert str(settings.llm_key_file) not in json.dumps(result)
    assert not settings.data_dir.exists()


def test_configuration_disabled_and_missing_are_distinct(settings):
    settings.llm_enabled = False
    assert configuration_status(settings)["configuration_state"] == "disabled"
    settings.llm_base_url = None
    assert configuration_status(settings)["configuration_state"] == "not_configured"
    settings.llm_base_url = BASE
    settings.llm_key_file.unlink()
    assert configuration_status(settings)["code"] == "model_key_missing"


def test_settings_environment_over_dotenv_over_toml(settings, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "SLEEPSCAPE_LLM_BASE_URL=https://dotenv.example.test/v1\nSLEEPSCAPE_LLM_ENABLED=false\n"
    )
    monkeypatch.setenv("SLEEPSCAPE_LLM_BASE_URL", BASE)
    monkeypatch.setenv("SLEEPSCAPE_LLM_ENABLED", "true")
    configured = Settings(
        _env_file=env, llm_key_file=settings.llm_key_file, llm_config_file=settings.llm_config_file
    )
    loaded = load_model_settings(configured)
    assert loaded.endpoint == BASE + "/chat/completions"
    assert loaded.enabled
    assert loaded.model == "qwen3.8-max"
    assert BASE not in repr(configured) + repr(loaded)
    assert PRIVATE not in repr(loaded)


@pytest.mark.parametrize(
    "base",
    [
        "http://models.example.test/v1",
        "https://user:secret@models.example.test/v1",
        "https://models.example.test/v1?api_key=synthetic",
        "https://models.example.test/v1#fragment",
        "https://models.example.test:99999/v1",
        "https:///v1",
        "https://models.example.test/v1\n",
        "https://models.example.test/v1/chat/completions",
        "https://models.example.test/a/../v1",
        "https://models.example.test/%0d/v1",
        "https://models.example.test/v1?",
    ],
)
def test_invalid_url_never_echoes_input(settings, base):
    settings.llm_base_url = base
    result = configuration_status(settings)
    assert result["configuration_state"] == "invalid_configuration"
    assert result["code"] == "model_configuration_invalid"
    assert base not in json.dumps(result)


def test_endpoint_mode_normalizes_once_and_rejects_both(settings):
    settings.llm_base_url = BASE + "/"
    assert load_model_settings(settings).endpoint == BASE + "/chat/completions"
    settings.llm_endpoint = BASE + "/chat/completions/"
    assert configuration_status(settings)["code"] == "model_configuration_invalid"
    settings.llm_base_url = None
    assert load_model_settings(settings).endpoint == BASE + "/chat/completions"
    settings.llm_endpoint = BASE
    assert configuration_status(settings)["code"] == "model_configuration_invalid"


@pytest.mark.parametrize(
    "replacement",
    [
        ("timeout_seconds = 180", "timeout_seconds = 181"),
        ("max_completion_tokens = 4096", "max_completion_tokens = 8192"),
        ("enable_thinking = false", 'enable_thinking = "private"'),
        ('provider = "openai_compatible"', 'provider = "private"'),
        ('model = "qwen3.8-max"', 'model = "https://private.example.test"'),
        ("schema_version = 1", "schema_version = true"),
    ],
)
def test_config_limits_and_types_are_safe(settings, replacement):
    settings.llm_config_file.write_text(settings.llm_config_file.read_text().replace(*replacement))
    assert configuration_status(settings)["code"] == "model_configuration_invalid"


def test_unknown_ref_and_broken_toml_do_not_leak(settings):
    settings.llm_model_ref = "unknown"
    assert configuration_status(settings)["code"] == "model_configuration_invalid"
    settings.llm_model_ref = "sleep_analyst"
    settings.llm_config_file.write_text("broken=" + PRIVATE)
    assert PRIVATE not in json.dumps(configuration_status(settings))


@pytest.mark.parametrize("file_mode,parent_mode", [(0o644, 0o700), (0o600, 0o755)])
def test_key_permissions_are_required(settings, file_mode, parent_mode):
    settings.llm_key_file.chmod(file_mode)
    settings.llm_key_file.parent.chmod(parent_mode)
    assert configuration_status(settings)["code"] == "model_key_permissions"


def test_key_symlink_is_rejected(settings):
    path = settings.llm_key_file
    real = path.with_name("actual")
    path.rename(real)
    path.symlink_to(real)
    assert configuration_status(settings)["code"] == "model_key_permissions"


@pytest.mark.parametrize(
    "raw", [b"", b"x" * 4097, b" private", b"one\ntwo", b"one\rHeader", b"\xff", b"one\n\n"]
)
def test_invalid_key_contents_are_never_used(settings, raw):
    settings.llm_key_file.write_bytes(raw)
    with pytest.raises(ModelError) as exc:
        create_client(load_model_settings(settings)).complete(request())
    assert str(exc.value) == "model_key_invalid"


def test_key_final_newline_allowed_and_repr_does_not_reveal_content(settings):
    settings.llm_key_file.write_text(PRIVATE + "\n")
    assert read_api_key(settings.llm_key_file) == PRIVATE
    assert PRIVATE not in repr(ChatMessage("user", PRIVATE))
    assert PRIVATE not in repr(ModelRequest([ChatMessage("user", PRIVATE)], SCHEMA))
    assert PRIVATE not in repr(ModelResult(PRIVATE, "qwen3.8-max", "stop"))


def test_config_fingerprint_tracks_key_and_config_changes(settings):
    first = configuration_fingerprint(settings)
    assert configuration_fingerprint(settings) == first
    settings.llm_key_file.write_text(PRIVATE + "-rotated")
    assert configuration_fingerprint(settings) != first
    second = configuration_fingerprint(settings)
    settings.llm_config_file.write_text(settings.llm_config_file.read_text().replace("180", "179"))
    assert configuration_fingerprint(settings) != second


def test_adapter_payload_boundaries_and_safe_result(settings, monkeypatch):
    value = answer()
    value["choices"][0]["message"]["reasoning_content"] = PRIVATE
    value["id"] = PRIVATE
    value["usage"].update(reasoning_tokens=PRIVATE, cache=PRIVATE)
    calls, response = transport(monkeypatch, value)
    result = create_client(load_model_settings(settings)).complete(
        request(max_completion_tokens=128)
    )
    assert json.loads(result.text) == {"ok": True, "note": "synthetic"}
    assert result.usage == {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13}
    assert PRIVATE not in repr(result) + json.dumps(result_metadata(result))
    assert len(calls) == 1 and calls[0][1] == 180
    call = calls[0][0]
    body = json.loads(call.data)
    assert call.full_url == BASE + "/chat/completions"
    assert call.get_header("Authorization") == "Bearer " + PRIVATE
    assert body["model"] == "qwen3.8-max" and body["stream"] is False
    assert body["max_completion_tokens"] == 128 and "max_tokens" not in body
    assert body["enable_thinking"] is False
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["type"] == "object"
    assert response.closed


def test_disabled_client_is_zero_network(settings):
    loaded = replace(load_model_settings(settings), enabled=False)
    with pytest.raises(ModelError, match="model_disabled"):
        create_client(loaded).complete(request())


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (302, "http_error", False),
        (401, "authentication_failed", False),
        (403, "permission_denied", False),
        (404, "model_unavailable", False),
        (429, "rate_limited", True),
        (500, "service_unavailable", True),
        (502, "service_unavailable", True),
        (503, "service_unavailable", True),
        (504, "service_unavailable", True),
        (422, "http_error", False),
    ],
)
def test_http_failures_are_bounded_and_redacted(settings, monkeypatch, status, code, retryable):
    stream = io.BytesIO(PRIVATE.encode())
    error = HTTPError(
        BASE,
        status,
        PRIVATE,
        {"Retry-After": "120", "Location": "https://other.example.test"},
        stream,
    )
    calls, _ = transport(monkeypatch, error=error)
    with pytest.raises(ModelError) as exc:
        create_client(load_model_settings(settings)).complete(request())
    assert exc.value.code == code and exc.value.retryable is retryable
    assert exc.value.http_status == status and exc.value.retry_after == 120
    assert len(calls) == 1 and stream.closed
    assert PRIVATE not in str(exc.value) + json.dumps(error_metadata(exc.value))


def test_redirect_handler_never_follows():
    assert (
        adapter.NoRedirect().redirect_request(
            None, None, 302, "secret", {}, "https://other.example.test"
        )
        is None
    )


def test_unsupported_schema_is_capability_failure(settings, monkeypatch):
    error = HTTPError(
        BASE,
        400,
        "private",
        {},
        io.BytesIO(b'{"error":{"message":"response_format json_schema unsupported"}}'),
    )
    calls, _ = transport(monkeypatch, error=error)
    with pytest.raises(ModelError, match="capability_unavailable"):
        create_client(load_model_settings(settings)).complete(request())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError(PRIVATE), "request_timeout"),
        (URLError(PRIVATE), "network_error"),
        (OSError(PRIVATE), "network_error"),
    ],
)
def test_network_errors_do_not_leak_or_retry(settings, monkeypatch, error, code):
    calls, _ = transport(monkeypatch, error=error)
    with pytest.raises(ModelError, match=code) as exc:
        create_client(load_model_settings(settings)).complete(request())
    assert len(calls) == 1 and exc.value.retryable
    assert PRIVATE not in str(exc.value)


@pytest.mark.parametrize(
    "raw", [b"{broken", b'{"model":"a","model":"b"}', b'{"value":NaN}', b'{"value":1e999}', b"\xff"]
)
def test_malformed_outer_json_is_rejected(settings, monkeypatch, raw):
    transport(monkeypatch, raw=raw)
    with pytest.raises(ModelError, match="invalid_json"):
        create_client(load_model_settings(settings)).complete(request())


@pytest.mark.parametrize(
    "raw,headers",
    [
        (b"x" * (256 * 1024 + 1), {}),
        (b"{}", {"Content-Length": str(256 * 1024 + 1)}),
        (b"{}", {"Content-Length": "not-a-number"}),
    ],
)
def test_response_size_checks_close_stream(settings, monkeypatch, raw, headers):
    _, response = transport(monkeypatch, raw=raw, headers=headers)
    with pytest.raises(ModelError, match="response_too_large"):
        create_client(load_model_settings(settings)).complete(request())
    assert response.closed


@pytest.mark.parametrize("model", ["other-model", "qwen3.8-max-unknown", None, PRIVATE])
def test_model_name_must_match_explicit_identity(settings, monkeypatch, model):
    transport(monkeypatch, answer(model=model))
    with pytest.raises(ModelError, match="model_mismatch"):
        create_client(load_model_settings(settings)).complete(request())


@pytest.mark.parametrize(
    "finish,code",
    [
        ("length", "output_truncated"),
        ("tool_calls", "invalid_response"),
        (PRIVATE, "invalid_response"),
        (None, "invalid_response"),
    ],
)
def test_incomplete_or_nontext_answers_cannot_succeed(settings, monkeypatch, finish, code):
    value = answer()
    value["choices"][0]["finish_reason"] = finish
    transport(monkeypatch, value)
    with pytest.raises(ModelError, match=code):
        create_client(load_model_settings(settings)).complete(request())


@pytest.mark.parametrize(
    "text,code",
    [
        ("{broken", "invalid_json"),
        ('{"ok":true,"note":"a","note":"b"}', "invalid_json"),
        ('{"ok":false,"note":"a"}', "output_schema_const"),
        ('{"ok":true,"note":"a","extra":"x"}', "output_schema_additional_properties"),
        ('{"ok":true,"note":null}', "output_schema_type"),
        ('{"ok":true}', "output_schema_required"),
        (json.dumps({"ok": True, "note": PRIVATE}), "invalid_response"),
        (json.dumps({"ok": True, "note": "models.example.test"}), "invalid_response"),
        ("", "invalid_response"),
    ],
)
def test_answer_json_schema_and_private_literals(settings, monkeypatch, text, code):
    value = answer()
    value["choices"][0]["message"]["content"] = text
    transport(monkeypatch, value)
    with pytest.raises(ModelError, match=code):
        create_client(load_model_settings(settings)).complete(request())


@pytest.mark.parametrize(
    "validator,code",
    [
        ("uniqueItems", "output_schema_unique_items"),
        ("contains", "output_schema_contains"),
        ("required", "output_schema_required"),
        ("const", "output_schema_const"),
        ("type", "output_schema_type"),
        ("enum", "output_schema_enum"),
        ("additionalProperties", "output_schema_additional_properties"),
        ("minItems", "output_schema_min_items"),
        ("maxItems", "output_schema_max_items"),
        ("minLength", "output_schema_min_length"),
        ("maxLength", "output_schema_max_length"),
        ("pattern", "output_schema_pattern"),
        ("format", "output_schema_format"),
        ("allOf", "output_schema_condition"),
        ("anyOf", "output_schema_condition"),
        ("oneOf", "output_schema_condition"),
        ("if", "output_schema_condition"),
        ("then", "output_schema_condition"),
        ("SYNTHETIC_SENSITIVE_VALIDATOR", "output_schema_other"),
        (None, "output_schema_other"),
    ],
)
def test_schema_diagnostic_exposes_only_registered_keyword(settings, monkeypatch, validator, code):
    transport(monkeypatch)

    def invalid(*args, **kwargs):
        raise ValidationError(
            "SYNTHETIC_SENSITIVE_VALIDATION_MESSAGE",
            validator=validator,
            instance={"SYNTHETIC_SENSITIVE_FIELD": "SYNTHETIC_SENSITIVE_VALUE"},
        )

    monkeypatch.setattr(adapter.Draft202012Validator, "validate", invalid)
    with pytest.raises(ModelError) as caught:
        create_client(load_model_settings(settings)).complete(request())
    error = caught.value
    assert error.code == str(error) == code
    metadata = error_metadata(error)
    assert metadata["code"] == code
    assert "SYNTHETIC_SENSITIVE" not in json.dumps(metadata)
    assert error.__cause__ is None and error.__suppress_context__ is True


@pytest.mark.parametrize(
    "constraint,value,code",
    [
        ({"type": "string", "enum": ["accepted"]}, "SYNTHETIC_VALUE", "output_schema_enum"),
        ({"type": "array", "minItems": 2}, ["SYNTHETIC_VALUE"], "output_schema_min_items"),
        (
            {"type": "array", "maxItems": 1},
            ["SYNTHETIC_VALUE", "SYNTHETIC_OTHER"],
            "output_schema_max_items",
        ),
        ({"type": "string", "minLength": 128}, "SYNTHETIC_VALUE", "output_schema_min_length"),
        ({"type": "string", "maxLength": 3}, "SYNTHETIC_VALUE", "output_schema_max_length"),
        ({"type": "string", "pattern": "^accepted$"}, "SYNTHETIC_VALUE", "output_schema_pattern"),
        ({"type": "string", "format": "email"}, "SYNTHETIC_VALUE", "output_schema_format"),
        (
            {"anyOf": [{"const": "accepted"}, {"type": "null"}]},
            "SYNTHETIC_VALUE",
            "output_schema_condition",
        ),
        (
            {"oneOf": [{"const": "accepted"}, {"type": "null"}]},
            "SYNTHETIC_VALUE",
            "output_schema_condition",
        ),
    ],
)
def test_schema_rule_diagnostics_keep_real_validation_strict(
    settings, monkeypatch, constraint, value, code
):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["synthetic_field"],
        "properties": {"synthetic_field": constraint},
    }
    reply = answer()
    reply["choices"][0]["message"]["content"] = json.dumps({"synthetic_field": value})
    transport(monkeypatch, reply)
    with pytest.raises(ModelError) as caught:
        create_client(load_model_settings(settings)).complete(request(output_schema=schema))
    assert caught.value.code == str(caught.value) == code
    assert "SYNTHETIC" not in json.dumps(error_metadata(caught.value))
    assert "synthetic_field" not in str(caught.value)


def test_non_validation_schema_exception_remains_generic(settings, monkeypatch):
    transport(monkeypatch)

    def invalid(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_SENSITIVE_INTERNAL_VALIDATION_MESSAGE")

    monkeypatch.setattr(adapter.Draft202012Validator, "validate", invalid)
    with pytest.raises(ModelError) as caught:
        create_client(load_model_settings(settings)).complete(request())
    assert caught.value.code == str(caught.value) == "output_schema_mismatch"
    assert "SYNTHETIC_SENSITIVE" not in json.dumps(error_metadata(caught.value))


def test_schema_diagnostic_codes_are_shared_and_business_failure_is_separate():
    assert len(OUTPUT_SCHEMA_ERRORS) == 37
    assert OUTPUT_SCHEMA_ERRORS <= ERROR_CODES
    assert "output_business_mismatch" in ERROR_CODES
    assert "output_business_mismatch" not in OUTPUT_SCHEMA_ERRORS


@pytest.mark.parametrize(
    "changes",
    [
        {"messages": []},
        {"messages": [ChatMessage("tool", "wrong")]},
        {"messages": [ChatMessage("user", "x" * 32769)]},
        {"max_completion_tokens": 4097},
        {"max_completion_tokens": True},
        {"output_schema": {"$defs": {"output": SCHEMA}}},
        {"output_schema": {**SCHEMA, "$ref": "https://private.example.test/schema"}},
        {"output_schema": {**SCHEMA, "$id": "https://private.example.test/schema"}},
        {"schema_name": "private / schema"},
        {"messages": [ChatMessage("user", PRIVATE)]},
        {"messages": [ChatMessage("user", "models.example.test")]},
    ],
)
def test_invalid_requests_are_zero_network(settings, changes):
    with pytest.raises(ModelError, match="invalid_model_request"):
        create_client(load_model_settings(settings)).complete(request(**changes))


def test_local_schema_references_supported(settings, monkeypatch):
    schema = {
        **SCHEMA,
        "$defs": {"note": {"type": "string"}},
        "properties": {"ok": {"const": True}, "note": {"$ref": "#/$defs/note"}},
    }
    transport(monkeypatch)
    assert (
        create_client(load_model_settings(settings))
        .complete(request(output_schema=schema))
        .finish_reason
        == "stop"
    )


@pytest.mark.parametrize(
    "profile,error",
    [
        ("native", "output_schema_mismatch"),
        ("aliyun-compatible-1", "capability_unavailable"),
    ],
)
def test_unresolved_local_schema_ref_is_safe(settings, monkeypatch, profile, error):
    schema = {**SCHEMA, "properties": {"ok": {"const": True}, "note": {"$ref": "#/$defs/missing"}}}
    calls, _ = transport(monkeypatch)
    with pytest.raises(ModelError, match=error):
        create_client(replace(load_model_settings(settings), schema_profile=profile)).complete(
            request(output_schema=schema)
        )
    assert len(calls) == (1 if profile == "native" else 0)


def test_retry_after_http_date_and_invalid_values():
    assert (
        29 <= adapter._retry_after(format_datetime(datetime.now(UTC) + timedelta(seconds=30))) <= 30
    )
    for value in (None, "private", "9" * 200, "9999999", "-1"):
        assert adapter._retry_after(value) is None


def test_usage_and_error_audit_are_whitelist_only():
    assert safe_usage(
        {"prompt_tokens": 4, "completion_tokens": True, "total_tokens": -1, "key": PRIVATE}
    ) == {"prompt_tokens": 4}
    assert safe_usage(PRIVATE) == {}
    error = ModelError(PRIVATE, retry_after=999999, http_status=True)
    assert error_metadata(error) == {
        "code": "model_operation_failed",
        "retryable": False,
        "http_status": None,
        "retry_after": None,
    }
