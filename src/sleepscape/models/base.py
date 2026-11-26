"""Small model protocol and safe typed errors, independent from sleep business logic."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

TRANSPORT_NORMALIZATIONS = frozenset({"none", "json_string_envelope", "single_object_array"})

OUTPUT_TYPE_ERRORS = frozenset(
    {
        "output_type_report",
        "output_type_report_string",
        "output_type_report_array",
        "output_type_report_array_empty",
        "output_type_report_array_multiple",
        "output_type_report_array_single_nonobject",
        "output_type_report_null",
        "output_type_report_scalar",
        "output_type_summary",
        "output_type_evidence_ids",
        "output_type_observations",
        "output_type_observation_item",
        "output_type_hypotheses",
        "output_type_hypothesis_item",
        "output_type_actions",
        "output_type_action_item",
        "output_type_alternatives",
        "output_type_limitations",
        "output_type_text",
        "output_type_observe",
        "output_type_identity",
    }
)

OUTPUT_SCHEMA_ERRORS = frozenset(
    {
        "output_schema_mismatch",
        "output_schema_unique_items",
        "output_schema_contains",
        "output_schema_required",
        "output_schema_const",
        "output_schema_type",
        "output_schema_enum",
        "output_schema_additional_properties",
        "output_schema_min_items",
        "output_schema_max_items",
        "output_schema_min_length",
        "output_schema_max_length",
        "output_schema_pattern",
        "output_schema_format",
        "output_schema_condition",
        "output_schema_other",
        *OUTPUT_TYPE_ERRORS,
    }
)

ERROR_CODES = frozenset(
    {
        "model_not_configured",
        "model_configuration_invalid",
        "model_key_missing",
        "model_key_permissions",
        "model_key_invalid",
        "model_disabled",
        "invalid_model_request",
        "capability_unavailable",
        "authentication_failed",
        "permission_denied",
        "model_unavailable",
        "rate_limited",
        "service_unavailable",
        "http_error",
        "network_error",
        "request_timeout",
        "response_too_large",
        "invalid_response",
        "invalid_json",
        *OUTPUT_SCHEMA_ERRORS,
        "output_business_mismatch",
        "output_truncated",
        "model_mismatch",
        "model_operation_failed",
        "model_busy",
        "budget_exhausted",
        "model_check_failed",
        "model_insufficient_input",
        "model_state_unavailable",
        "model_deadline_exceeded",
        "model_worker_failed",
        "storage_capacity_critical",
        "storage_capacity_restricted",
        "storage_capacity_unavailable",
    }
)


class ModelError(RuntimeError):
    """Only registered codes and numeric metadata may cross the public boundary."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.code = (
            code if isinstance(code, str) and code in ERROR_CODES else "model_operation_failed"
        )
        self.retryable = retryable is True
        self.http_status = (
            http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        )
        self.retry_after = (
            retry_after if type(retry_after) is int and 0 <= retry_after <= 86400 else None
        )
        super().__init__(self.code)


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str = field(repr=False)


@dataclass(frozen=True)
class ModelRequest:
    messages: list[ChatMessage] = field(repr=False)
    output_schema: dict = field(repr=False)
    schema_name: str = "sleep_response"
    max_completion_tokens: int | None = None
    exchange_path: Path | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ModelResult:
    text: str = field(repr=False)
    model: str
    finish_reason: str
    usage: dict[str, int] = field(default_factory=dict)
    transport_normalization: Literal["none", "json_string_envelope", "single_object_array"] = "none"


class ModelClient(Protocol):
    def complete(self, request: ModelRequest) -> ModelResult: ...
