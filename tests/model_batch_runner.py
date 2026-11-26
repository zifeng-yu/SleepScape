"""Evaluate synthetic model responses; callers own live authorization and durable budgets.

This module never loads credentials or opens a network connection. An explicitly
provided ``invoke`` performs at most one authorized call for each synthetic case.
Only fixed status metadata is returned or passed to the optional persistence sink.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import statistics
import threading
import time
from collections import Counter

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from sleepscape.domain.sleep_features import SleepAnalysisError
from sleepscape.domain.weekly_review import night_observations
from sleepscape.models.audit import safe_usage
from sleepscape.models.base import (
    ERROR_CODES,
    OUTPUT_SCHEMA_ERRORS,
    TRANSPORT_NORMALIZATIONS,
    ModelError,
    ModelResult,
)
from sleepscape.models.openai_compatible import MAX_RESPONSE_BYTES, strict_json
from sleepscape.resources import contract_schema, validate_model_output, validate_sleep_input
from sleepscape.services.sleep_analysis import _request

MAX_CASES = 50
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_OUTCOMES = {"passed", "schema_failed", "business_failed", "protocol_failed", "transport_failed"}
_TRANSPORT_ERRORS = {
    "network_error",
    "request_timeout",
    "model_deadline_exceeded",
    "rate_limited",
    "service_unavailable",
    "http_error",
    "authentication_failed",
    "permission_denied",
    "model_unavailable",
}
_ROW_KEYS = {
    "case_id",
    "scenario",
    "input_hash",
    "template_sha256",
    "model_schema_sha256",
    "outcome",
    "json_pass",
    "schema_pass",
    "business_pass",
    "error_code",
    "duration_ms",
    "usage",
    "transport_normalization",
}


class BatchEvaluationError(ValueError):
    """Fixed diagnostics only; never propagate untrusted response or callback text."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _template(request, payload):
    if (
        len(request.messages) != 2
        or [m.role for m in request.messages] != ["system", "user"]
        or strict_json(request.messages[1].content)
        != {"target_date": payload["night_date"], "nights": [night_observations(payload)]}
    ):
        raise BatchEvaluationError("batch_request_template_invalid")
    return {
        "messages": [
            {"role": request.messages[index].role, "content": request.messages[index].content}
            for index in (0,)
        ],
        "schema_name": request.schema_name,
        "schema": request.output_schema,
        "max_completion_tokens": request.max_completion_tokens,
    }


def _schema_error(keyword):
    return {
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
    }.get(keyword, "output_schema_other")


class BatchEvaluator:
    """Freeze one template; evaluate each unique synthetic case at most once.

    ``invoke(case, request)`` must reserve its own durable authorized allowance
    before it calls the production model adapter. This class neither changes nor
    bypasses a production budget. ``existing_results`` contains previously saved
    safe rows; a task launcher must also guard reserved-but-interrupted calls.
    """

    def __init__(self, cases, invoke, *, sink=None, existing_results=None):
        if not callable(invoke) or (sink is not None and not callable(sink)):
            raise BatchEvaluationError("batch_callback_invalid")
        if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
            raise BatchEvaluationError("batch_case_count_invalid")
        self._cases = copy.deepcopy(cases)
        self._requests = []
        self._invoke = invoke
        self._sink = sink
        self._rows = {}
        self._lock = threading.RLock()
        seen_ids, seen_hashes, templates = set(), set(), []
        try:
            for case in self._cases:
                if set(case) != {"case_id", "scenario", "payload"}:
                    raise BatchEvaluationError("batch_case_invalid")
                if any(
                    not isinstance(case[key], str) or not _LABEL.fullmatch(case[key])
                    for key in ("case_id", "scenario")
                ):
                    raise BatchEvaluationError("batch_case_invalid")
                payload = case["payload"]
                if payload.get("synthetic") is not True:
                    raise BatchEvaluationError("batch_requires_synthetic_input")
                validate_sleep_input(payload)
                if case["case_id"] in seen_ids or payload["input_hash"] in seen_hashes:
                    raise BatchEvaluationError("batch_cases_not_unique")
                seen_ids.add(case["case_id"])
                seen_hashes.add(payload["input_hash"])
                request = _request(payload, correction=False)
                templates.append(_template(request, payload))
                self._requests.append(copy.deepcopy(request))
            if any(template != templates[0] for template in templates[1:]):
                raise BatchEvaluationError("batch_request_template_changed")
            schema = contract_schema("model_output")
            if any(request.output_schema != schema for request in self._requests):
                raise BatchEvaluationError("batch_schema_changed")
            Draft202012Validator.check_schema(schema)
        except BatchEvaluationError:
            raise
        except Exception:
            raise BatchEvaluationError("batch_input_invalid") from None
        self.template_sha256 = _digest(templates[0])
        self.model_schema_sha256 = _digest(schema)
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self._by_id = {case["case_id"]: index for index, case in enumerate(self._cases)}
        if existing_results is not None:
            if not isinstance(existing_results, list):
                raise BatchEvaluationError("batch_resume_invalid")
            for row in existing_results:
                self._restore(row)

    @property
    def case_count(self):
        return len(self._cases)

    @property
    def rows(self):
        with self._lock:
            return [copy.deepcopy(self._rows[index]) for index in sorted(self._rows)]

    def _base_row(self, index):
        case = self._cases[index]
        return {
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "input_hash": case["payload"]["input_hash"],
            "template_sha256": self.template_sha256,
            "model_schema_sha256": self.model_schema_sha256,
            "outcome": "protocol_failed",
            "json_pass": None,
            "schema_pass": None,
            "business_pass": None,
            "error_code": None,
            "duration_ms": 0,
            "usage": {},
            "transport_normalization": None,
        }

    def _restore(self, row):
        try:
            if not isinstance(row, dict) or set(row) != _ROW_KEYS:
                raise ValueError
            index = self._by_id[row["case_id"]]
            expected = self._base_row(index)
            for name in (
                "case_id",
                "scenario",
                "input_hash",
                "template_sha256",
                "model_schema_sha256",
            ):
                if row[name] != expected[name]:
                    raise ValueError
            if index in self._rows or row["outcome"] not in _OUTCOMES:
                raise ValueError
            for name in ("json_pass", "schema_pass", "business_pass"):
                if row[name] is not None and type(row[name]) is not bool:
                    raise ValueError
            if row["error_code"] is not None and row["error_code"] not in ERROR_CODES:
                raise ValueError
            if (
                type(row["duration_ms"]) is not int
                or not 0 <= row["duration_ms"] <= 86_400_000
                or not isinstance(row["usage"], dict)
                or safe_usage(row["usage"]) != row["usage"]
                or row["transport_normalization"] not in TRANSPORT_NORMALIZATIONS | {None}
            ):
                raise ValueError
            if row["outcome"] == "passed":
                if row["error_code"] is not None or any(
                    row[name] is not True for name in ("json_pass", "schema_pass", "business_pass")
                ):
                    raise ValueError
            elif row["error_code"] is None or row["business_pass"] is True:
                raise ValueError
            if row["schema_pass"] is True and row["json_pass"] is not True:
                raise ValueError
            self._rows[index] = copy.deepcopy(row)
        except Exception:
            raise BatchEvaluationError("batch_resume_invalid") from None

    @staticmethod
    def _model_failure(row, error):
        row["error_code"] = error.code
        if error.code in OUTPUT_SCHEMA_ERRORS:
            row.update(outcome="schema_failed", json_pass=True, schema_pass=False)
        elif error.code == "invalid_json":
            row.update(outcome="protocol_failed", json_pass=False)
        else:
            row["outcome"] = (
                "transport_failed" if error.code in _TRANSPORT_ERRORS else "protocol_failed"
            )

    def _evaluate(self, index, row):
        try:
            reply = self._invoke(
                copy.deepcopy(self._cases[index]), copy.deepcopy(self._requests[index])
            )
        except ModelError as error:
            self._model_failure(row, error)
            return
        if not isinstance(reply, ModelResult):
            self._model_failure(row, ModelError("invalid_response"))
            return
        row["usage"] = safe_usage(reply.usage)
        if reply.transport_normalization not in TRANSPORT_NORMALIZATIONS:
            self._model_failure(row, ModelError("invalid_response"))
            return
        row["transport_normalization"] = reply.transport_normalization
        if not isinstance(reply.text, str) or len(reply.text.encode()) > MAX_RESPONSE_BYTES:
            self._model_failure(row, ModelError("response_too_large"))
            return
        try:
            candidate = strict_json(reply.text)
        except ModelError as error:
            self._model_failure(row, error)
            return
        row["json_pass"] = True
        try:
            self._validator.validate(candidate)
        except ValidationError as error:
            row.update(
                outcome="schema_failed",
                schema_pass=False,
                error_code=_schema_error(error.validator),
            )
            return
        row["schema_pass"] = True
        try:
            validate_model_output(candidate, self._cases[index]["payload"])
        except SleepAnalysisError:
            row.update(
                outcome="business_failed",
                business_pass=False,
                error_code="output_business_mismatch",
            )
            return
        row.update(outcome="passed", business_pass=True)

    def run_case(self, index):
        if type(index) is not int or not 0 <= index < self.case_count:
            raise BatchEvaluationError("batch_case_index_invalid")
        with self._lock:
            if index in self._rows:
                return copy.deepcopy(self._rows[index])
            row = self._base_row(index)
            started = time.monotonic()
            interrupted = False
            try:
                self._evaluate(index, row)
            except (KeyboardInterrupt, SystemExit):
                interrupted = True
                self._model_failure(row, ModelError("model_worker_failed"))
            except Exception:
                self._model_failure(row, ModelError("model_operation_failed"))
            row["duration_ms"] = min(86_400_000, max(0, int((time.monotonic() - started) * 1000)))
            # Cache before persistence; even a failing sink must never invoke twice.
            self._rows[index] = copy.deepcopy(row)
            if self._sink is not None:
                try:
                    self._sink(copy.deepcopy(row))
                except Exception:
                    raise BatchEvaluationError("batch_sink_failed") from None
            if interrupted:
                raise KeyboardInterrupt from None
            return copy.deepcopy(row)

    def summary(self):
        rows = self.rows
        elapsed = sorted(row["duration_ms"] for row in rows)
        usage = Counter()
        for row in rows:
            usage.update(row["usage"])
        schema_pass = sum(row["schema_pass"] is True for row in rows)
        business_pass = sum(row["business_pass"] is True for row in rows)
        return {
            "requested_cases": self.case_count,
            "evaluated_cases": len(rows),
            "remaining_cases": self.case_count - len(rows),
            "template_sha256": self.template_sha256,
            "model_schema_sha256": self.model_schema_sha256,
            "json_pass_count": sum(row["json_pass"] is True for row in rows),
            "json_fail_count": sum(row["json_pass"] is False for row in rows),
            "json_unknown_count": sum(row["json_pass"] is None for row in rows),
            "schema_pass_count": schema_pass,
            "schema_fail_count": sum(row["schema_pass"] is False for row in rows),
            "schema_unknown_count": sum(row["schema_pass"] is None for row in rows),
            "business_pass_count": business_pass,
            "business_fail_count": sum(row["business_pass"] is False for row in rows),
            "business_unknown_count": sum(row["business_pass"] is None for row in rows),
            "schema_pass_rate_of_requested": schema_pass / self.case_count,
            "business_pass_rate_of_requested": business_pass / self.case_count,
            "outcomes": dict(sorted(Counter(row["outcome"] for row in rows).items())),
            "error_codes": dict(
                sorted(Counter(row["error_code"] for row in rows if row["error_code"]).items())
            ),
            "transport_normalizations": dict(
                sorted(
                    Counter(
                        row["transport_normalization"]
                        for row in rows
                        if row["transport_normalization"] is not None
                    ).items()
                )
            ),
            "known_usage": dict(sorted(usage.items())),
            "cases_with_known_usage": sum(bool(row["usage"]) for row in rows),
            "usage_excludes_unknown_failures": True,
            "elapsed_ms": {
                "min": elapsed[0] if elapsed else None,
                "median": statistics.median(elapsed) if elapsed else None,
                "p95_nearest_rank": elapsed[math.ceil(len(elapsed) * 0.95) - 1]
                if elapsed
                else None,
                "max": elapsed[-1] if elapsed else None,
            },
        }
