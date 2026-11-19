"""Metadata allowlists; this layer never stores prompts, bodies, URLs, keys or reasoning."""

from sleepscape.models.base import ModelError, ModelResult

USAGE_FIELDS = frozenset({"prompt_tokens", "completion_tokens", "total_tokens"})


def safe_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: number
        for key, number in value.items()
        if key in USAGE_FIELDS and type(number) is int and 0 <= number <= 2**53 - 1
    }


def result_metadata(result: ModelResult) -> dict:
    # Client validates identity against configured names. Arbitrary provider
    # strings and all answer content stay out of generic metadata snapshots.
    return {"finish_reason": "stop", "usage": safe_usage(result.usage)}


def error_metadata(error: ModelError) -> dict:
    safe = ModelError(
        error.code,
        retryable=error.retryable,
        http_status=error.http_status,
        retry_after=error.retry_after,
    )
    return {
        "code": safe.code,
        "retryable": safe.retryable,
        "http_status": safe.http_status,
        "retry_after": safe.retry_after,
    }
