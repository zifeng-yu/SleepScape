"""One authored-answer schema for nightly and rolling reports."""

import hashlib
from datetime import date, timedelta
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from sleepscape.domain.sleep_features import FACT_REGISTRY, LIMITATIONS, canonical_json
from sleepscape.models.base import ChatMessage, ModelError, ModelRequest
from sleepscape.models.openai_compatible import MAX_MESSAGE_BYTES, strict_json
from sleepscape.resources import contract_schema, system_prompt, validate_answer

MAX_EVIDENCE_BYTES = 24 * 1024


@lru_cache(maxsize=1)
def _schemas():
    root = files("sleepscape.resources")
    return strict_json(root.joinpath("sleep-review.schema.json").read_text()), root.joinpath(
        "sleep-review-system.txt"
    ).read_text()


@lru_cache(maxsize=3)
def _validator(kind):
    return Draft202012Validator(_schemas()[0][kind], format_checker=FormatChecker())


def resource_digest():
    return hashlib.sha256(
        canonical_json([_schemas(), contract_schema("model_output")]).encode()
    ).hexdigest()


def validate_evidence(value, *, for_generation=False):
    try:
        current = "target_date" in value
        if for_generation and not current:
            raise ValueError
        _validator("observations" if current else "input").validate(value)
        if current:
            start = date.fromisoformat(value["window"]["start_date"])
            days = [n["night_date"] for n in value["nights"]]
            if (
                days != [(start + timedelta(days=i)).isoformat() for i in range(len(days))]
                or value["window"]["end_date"] != days[-1]
                or value["target_date"] != days[-1]
                or len(days) != value["window"]["visible_n"]
                or len(days) != value["window"]["eligible_n"]
            ):
                raise ValueError
            for night in value["nights"]:
                if not set(night["limits"]) <= LIMITATIONS:
                    raise ValueError
                for metric, entry in night["measurements"].items():
                    unit, _, period, lo, hi = FACT_REGISTRY[metric]
                    if (
                        period not in ("night", "previous_day")
                        or unit != entry["unit"]
                        or not lo <= entry["value"] <= hi
                    ):
                        raise ValueError
        return value
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError):
        raise ModelError("model_operation_failed") from None


def observation_request(value, *, weekly=False, correction=False):
    schema = contract_schema("model_output")
    prompt = _schemas()[1] if weekly else system_prompt() + "\n只解读目标夜，comparison 填 null。"
    prompt += "\n" + canonical_json(schema)
    messages = [
        ChatMessage("system", prompt),
        ChatMessage(
            "user", canonical_json({k: value[k] for k in ("window", "nights")} if weekly else value)
        ),
    ]
    if correction:
        messages.append(ChatMessage("user", "请重新按给定 JSON Schema 返回完整对象。"))
    if (
        len(messages[1].content.encode()) > MAX_EVIDENCE_BYTES
        or sum(len(m.content.encode()) for m in messages) > MAX_MESSAGE_BYTES
    ):
        raise ValueError("review_input_too_large")
    return ModelRequest(
        messages,
        schema,
        schema_name="sleepscape_sleep_review" if weekly else "sleepscape_sleep_report",
    )


def request_for(value, *, correction=False):
    return observation_request(
        validate_evidence(value, for_generation=True), weekly=True, correction=correction
    )


def validate_output(value, evidence):
    if "target_date" in evidence:
        return validate_answer(value)
    try:
        _validator("output").validate(value)
    except ValidationError:
        raise ModelError("output_schema_mismatch") from None
    return value


def parse_output(raw, evidence):
    return validate_answer(strict_json(raw))
