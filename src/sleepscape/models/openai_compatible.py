"""Bounded OpenAI-compatible JSON requests, without redirects, retries or data access."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from sleepscape.models.audit import safe_usage
from sleepscape.models.base import ChatMessage, ModelError, ModelRequest, ModelResult
from sleepscape.models.schema_transport import SCHEMA_PROFILES, transport_schema
from sleepscape.models.settings import RESPONSE_MODES, ModelSettings, read_api_key
from sleepscape.storage.model_exchange import write_event

MAX_RESPONSE_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 32 * 1024


def _sleep_report_type_error(path) -> str:
    """Classify public report positions; never return a path, index or model value."""
    fallback = "output_schema_type"
    parts = tuple(path)
    if len(parts) > 4 or any(type(part) not in (str, int) for part in parts):
        return fallback
    if not parts:
        return "output_type_report"
    if len(parts) == 1:
        return {
            "summary": "output_type_summary",
            "observations": "output_type_observations",
            "hypotheses": "output_type_hypotheses",
            "actions": "output_type_actions",
            "limitations": "output_type_limitations",
            "night_date": "output_type_identity",
            "input_hash": "output_type_identity",
        }.get(parts[0], fallback)

    def index(value):
        return type(value) is int and value >= 0

    group = parts[0]
    if group == "limitations" and len(parts) == 2 and index(parts[1]):
        return "output_type_limitations"
    if group == "summary":
        child = parts[1:]
    elif group in {"observations", "hypotheses", "actions"} and index(parts[1]):
        if len(parts) == 2:
            return {
                "observations": "output_type_observation_item",
                "hypotheses": "output_type_hypothesis_item",
                "actions": "output_type_action_item",
            }[group]
        child = parts[2:]
    else:
        return fallback
    if child == ("text",):
        return "output_type_text"
    if group == "actions" and child == ("observe",):
        return "output_type_observe"
    if len(child) == 1 or (len(child) == 2 and index(child[1])):
        if child[0] == "evidence_ids":
            return "output_type_evidence_ids"
        if group == "hypotheses" and child[0] == "alternatives":
            return "output_type_alternatives"
    return fallback


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _no_constant(value):
    raise ValueError("nonfinite_json_number")


def strict_json(value: str | bytes) -> object:
    try:
        result = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_no_constant)
        # The parser turns 1e999 into infinity without parse_constant; encoding
        # with allow_nan=False rejects it and nested non-finite numbers too.
        json.dumps(result, allow_nan=False)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ModelError("invalid_json") from None


def _validate_schema(schema: object) -> dict:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ModelError("invalid_model_request")
    try:
        if len(json.dumps(schema, allow_nan=False).encode()) > MAX_REQUEST_BYTES:
            raise ValueError
        # Remote references could cause network access during local validation.
        # Only local JSON pointers within the caller-supplied document are legal.
        pending = [schema]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in {"$id", "$anchor", "$dynamicAnchor", "$recursiveRef"}:
                        raise ValueError
                    if key in {"$ref", "$dynamicRef"} and (
                        not isinstance(value, str) or not value.startswith("#/")
                    ):
                        raise ValueError
                    pending.append(value)
            elif isinstance(item, list):
                pending.extend(item)
        Draft202012Validator.check_schema(schema)
        if schema.get("additionalProperties") is not False:
            raise ValueError
        if set(schema.get("required", [])) != set(schema.get("properties", {})):
            raise ValueError
        return schema
    except (ValueError, TypeError, RecursionError, SchemaError):
        raise ModelError("invalid_model_request") from None


def _payload(settings: ModelSettings, request: ModelRequest) -> tuple[bytes, dict]:
    try:
        if not isinstance(request, ModelRequest):
            raise ValueError
        if not isinstance(request.messages, list) or not 1 <= len(request.messages) <= 8:
            raise ValueError
        messages = []
        for item in request.messages:
            if (
                not isinstance(item, ChatMessage)
                or item.role not in {"system", "user", "assistant"}
                or not isinstance(item.content, str)
                or not item.content.strip()
            ):
                raise ValueError
            messages.append({"role": item.role, "content": item.content})
        if sum(len(item["content"].encode("utf-8")) for item in messages) > MAX_MESSAGE_BYTES:
            raise ValueError
        tokens = request.max_completion_tokens
        if tokens is None:
            tokens = settings.max_completion_tokens
        if type(tokens) is not int or not 1 <= tokens <= settings.max_completion_tokens <= 4096:
            raise ValueError
        if (
            not isinstance(request.schema_name, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", request.schema_name) is None
        ):
            raise ValueError
        schema = _validate_schema(request.output_schema)
        if (
            not isinstance(settings.response_mode, str)
            or settings.response_mode not in RESPONSE_MODES
            or not isinstance(settings.schema_profile, str)
            or settings.schema_profile not in SCHEMA_PROFILES
        ):
            raise ModelError("model_configuration_invalid")
        if settings.response_mode == "json_object":
            if not any(
                item["role"] in {"system", "user"} and "json" in item["content"].lower()
                for item in messages
            ):
                raise ModelError("invalid_model_request")
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": transport_schema(schema, settings.schema_profile),
                },
            }
        payload = {
            "model": settings.model,
            "messages": messages,
            "stream": False,
            "max_completion_tokens": tokens,
            "enable_thinking": settings.enable_thinking,
            "response_format": response_format,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError
        return encoded, schema
    except ModelError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ModelError("invalid_model_request") from None


def _retry_after(value: object) -> int | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        if value.isascii() and value.isdecimal():
            seconds = int(value)
        else:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                return None
            seconds = max(0, math.ceil((when - datetime.now(UTC)).total_seconds()))
        return seconds if 0 <= seconds <= 86400 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _http_error(error: HTTPError) -> ModelError:
    code = error.code
    retry_after = _retry_after(error.headers.get("Retry-After")) if error.headers else None
    try:
        classification = {
            401: "authentication_failed",
            403: "permission_denied",
            404: "model_unavailable",
            429: "rate_limited",
            500: "service_unavailable",
            502: "service_unavailable",
            503: "service_unavailable",
            504: "service_unavailable",
        }.get(code, "http_error")
        if code == 400:
            # Error bodies are examined only for fixed capability markers. They
            # never become exception messages, audit values or next prompts.
            raw = error.read(4097)
            if len(raw) <= 4096:
                text = raw.decode("utf-8", errors="replace").lower()
                if any(
                    marker in text
                    for marker in ("json_schema", "response_format", "enable_thinking")
                ):
                    classification = "capability_unavailable"
        return ModelError(
            classification,
            http_status=code,
            retryable=code in {429, 500, 502, 503, 504},
            retry_after=retry_after,
        )
    except (OSError, HTTPException):
        return ModelError("http_error", http_status=code)
    finally:
        error.close()


def _log_answer(request, result, key, host):
    if request.exchange_path is None:
        return
    choices = result.get("choices")
    answers = []
    for choice in choices if isinstance(choices, list) else []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            answer = {
                k: message[k] for k in ("content", "refusal") if isinstance(message.get(k), str)
            }
            for k, value in answer.items():
                if key in value or host.lower() in value.lower():
                    answer[k] = "[redacted]"
            reason = choice.get("finish_reason")
            answer["finish_reason"] = (
                reason if reason in ("stop", "length", "content_filter", "tool_calls") else None
            )
            answers.append(answer)
    write_event(
        request,
        "response",
        {"http_status": 200, "answers": answers, "usage": safe_usage(result.get("usage"))},
    )


class OpenAICompatibleClient:
    def __init__(self, settings: ModelSettings) -> None:
        self.settings = settings

    def complete(self, request: ModelRequest) -> ModelResult:
        if not self.settings.enabled:
            raise ModelError("model_disabled")
        payload, schema = _payload(self.settings, request)
        key = read_api_key(self.settings.key_file)
        try:
            # Credentials belong exclusively in the Authorization header. They
            # must not be echoed through messages or accepted model answers.
            host = urlsplit(self.settings.endpoint).hostname or ""
            if any(
                key in item.content or host.lower() in item.content.lower()
                for item in request.messages
            ):
                raise ModelError("invalid_model_request")
            write_event(request, "request", json.loads(payload))
            call = Request(
                self.settings.endpoint,
                data=payload,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                method="POST",
            )
            with build_opener(NoRedirect).open(
                call, timeout=self.settings.timeout_seconds
            ) as response:
                if response.getcode() != 200:
                    raise ModelError("http_error", http_status=response.getcode())
                if response.geturl() != self.settings.endpoint:
                    raise ModelError("invalid_response")
                declared = response.headers.get("Content-Length")
                if declared is not None and (
                    not declared.isascii()
                    or not declared.isdecimal()
                    or len(declared) > 12
                    or int(declared) > MAX_RESPONSE_BYTES
                ):
                    raise ModelError("response_too_large")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ModelError("response_too_large")
            result = strict_json(raw)
            if not isinstance(result, dict):
                raise ModelError("invalid_response")
            _log_answer(request, result, key, host)
            if result.get("model") not in self.settings.accepted_models:
                raise ModelError("model_mismatch")
            choices = result.get("choices")
            if (
                not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0], dict)
            ):
                raise ModelError("invalid_response")
            choice = choices[0]
            if choice.get("finish_reason") == "length":
                raise ModelError("output_truncated")
            if choice.get("finish_reason") != "stop":
                raise ModelError("invalid_response")
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("tool_calls") or message.get("refusal"):
                raise ModelError("invalid_response")
            text = message.get("content")
            if not isinstance(text, str) or not text.strip():
                raise ModelError("invalid_response")
            parsed = strict_json(text)
            normalization = "none"
            if isinstance(parsed, str):
                try:
                    inner = strict_json(parsed)
                except ModelError:
                    inner = None
                if isinstance(inner, dict):
                    parsed = inner
                    normalization = "json_string_envelope"
            elif isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                parsed = parsed[0]
                normalization = "single_object_array"
            canonical = json.dumps(
                parsed, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            if key in canonical or host.lower() in canonical.lower():
                raise ModelError("invalid_response")
            try:
                Draft202012Validator(schema, format_checker=FormatChecker()).validate(parsed)
            except ValidationError as exc:
                # Only the trusted validator keyword selects a fixed diagnostic.
                # Never retain its message, instance, schema path or field values.
                keyword = exc.validator if isinstance(exc.validator, str) else ""
                code = {
                    "uniqueItems": "output_schema_unique_items",
                    "contains": "output_schema_contains",
                    "required": "output_schema_required",
                    "const": "output_schema_const",
                    "type": "output_schema_type",
                    "enum": "output_schema_enum",
                    "additionalProperties": "output_schema_additional_properties",
                    "minItems": "output_schema_min_items",
                    "maxItems": "output_schema_max_items",
                    "minLength": "output_schema_min_length",
                    "maxLength": "output_schema_max_length",
                    "pattern": "output_schema_pattern",
                    "format": "output_schema_format",
                    "allOf": "output_schema_condition",
                    "anyOf": "output_schema_condition",
                    "oneOf": "output_schema_condition",
                    "if": "output_schema_condition",
                    "then": "output_schema_condition",
                }.get(keyword, "output_schema_other")
                if keyword == "type" and request.schema_name == "sleepscape_sleep_report":
                    code = _sleep_report_type_error(exc.absolute_path)
                    if code == "output_type_report":
                        if isinstance(exc.instance, str):
                            code = "output_type_report_string"
                        elif isinstance(exc.instance, list):
                            if len(exc.instance) == 0:
                                code = "output_type_report_array_empty"
                            elif len(exc.instance) > 1:
                                code = "output_type_report_array_multiple"
                            else:
                                code = "output_type_report_array_single_nonobject"
                        elif exc.instance is None:
                            code = "output_type_report_null"
                        elif type(exc.instance) in (bool, int, float):
                            code = "output_type_report_scalar"
                raise ModelError(code) from None
            except Exception:
                # Includes unresolved local references; the schema and response
                # are untrusted and their exception text must not escape.
                raise ModelError("output_schema_mismatch") from None
            # No reasoning_content, request ID, headers or provider body is returned.
            return ModelResult(
                canonical if normalization != "none" else text,
                result["model"],
                "stop",
                safe_usage(result.get("usage")),
                transport_normalization=normalization,
            )
        except ModelError:
            raise
        except HTTPError as exc:
            error = _http_error(exc)
            write_event(request, "response", {"http_status": error.http_status, "code": error.code})
            raise error from None
        except TimeoutError:
            raise ModelError("request_timeout", retryable=True) from None
        except (URLError, OSError, HTTPException):
            raise ModelError("network_error", retryable=True) from None
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
            raise ModelError("invalid_response") from None
        finally:
            key = ""
