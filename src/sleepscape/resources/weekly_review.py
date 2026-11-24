"""Packaged contract for the scheduled rolling sleep review."""

import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from sleepscape.domain.sleep_features import (
    canonical_json,
    opens_with_collection_status,
    report_text_is_safe,
)
from sleepscape.domain.weekly_review import METRIC_UNITS
from sleepscape.models.base import ChatMessage, ModelError, ModelRequest
from sleepscape.models.openai_compatible import MAX_MESSAGE_BYTES, strict_json

MAX_EVIDENCE_BYTES = 24 * 1024
_COMPARATIVE = re.compile(r"相比|相较|高于|低于|长于|短于|更高|更低|更长|更短|组间|差值")
_UNSUPPORTED = re.compile(r"长期趋势|持续恶化|导致|造成了|吵醒了|引发了")
_ENVIRONMENT_TERMS = {
    "温度": "temperature_c",
    "湿度": "relative_humidity_pct",
    "照度": "illuminance_lux",
    "光照": "illuminance_lux",
    "亮度": "illuminance_lux",
    "声音": "sound_relative",
    "音量": "sound_relative",
    "噪声": "sound_relative",
}


@lru_cache(maxsize=1)
def _schemas():
    root = files("sleepscape.resources")
    schema = json.loads(root.joinpath("sleep-review.schema.json").read_text())
    prompt = root.joinpath("sleep-review-system.txt").read_text()
    for contract in schema.values():
        Draft202012Validator.check_schema(contract)
    return schema, prompt


@lru_cache(maxsize=2)
def _validator(kind):
    return Draft202012Validator(_schemas()[0][kind], format_checker=FormatChecker())


def resource_digest():
    schema, prompt = _schemas()
    return hashlib.sha256(canonical_json([schema, prompt]).encode()).hexdigest()


def validate_evidence(value):
    try:
        _validator("input").validate(value)
        nights = value["nights"]
        span = value["window"]
        start = date.fromisoformat(span["start_date"])
        days = [(start + timedelta(days=i)).isoformat() for i in range(len(nights))]
        identities = {
            item["id"] for key in ("nights", "aggregates", "comparisons") for item in value[key]
        }
        expected_ids = sum(len(value[key]) for key in ("nights", "aggregates", "comparisons"))
        feeling = value["quality"]["feeling_state"]
        counts = value["quality"]["feeling_counts"]
        measured = Counter(item["feeling"] or "unfilled" for item in nights)
        if (
            span["visible_n"] != len(nights)
            or span["eligible_n"] != len(nights)
            or [item["night_date"] for item in nights] != days
            or span["end_date"] != days[-1]
            or len(identities) != expected_ids
            or not set(value["examples"]) <= {item["id"] for item in nights}
            or any(set(night["metrics"]) - METRIC_UNITS.keys() for night in nights)
            or any(
                row["unit"] != METRIC_UNITS[row["metric"]]
                for key in ("aggregates", "comparisons")
                for row in value[key]
            )
            or (feeling == "available") != (counts is not None)
            or (counts is not None and any(n != measured[key] for key, n in counts.items()))
            or (counts is None and any(item["feeling"] is not None for item in nights))
            or any(row["valid_n"] > len(nights) for row in value["aggregates"])
            or any(row["poor_n"] + row["good_n"] > len(nights) for row in value["comparisons"])
        ):
            raise ValueError
        return value
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError):
        raise ModelError("model_operation_failed") from None


def request_for(value, *, correction=False):
    encoded = canonical_json(validate_evidence(value))
    schema, prompt = _schemas()
    output = schema["output"]
    instructions = prompt + "\n\n" + canonical_json(output)
    if correction:
        instructions += (
            "\n上次输出未通过校验。只保留主要发现和一条有直接证据的观察，hypotheses 填 []。"
            "actions 有依据时保留一条，写清试什么或记录什么、如何对照，否则填 []。"
            "每条仅引用一至三个直接有效的 ID，摘要须有睡眠证据。"
            "输出规定的四字段JSON对象，正文不要写数字或推断因果。"
        )
    if len(encoded.encode()) > MAX_EVIDENCE_BYTES:
        raise ValueError("review_input_too_large")
    if len(instructions.encode()) + len(encoded.encode()) > MAX_MESSAGE_BYTES:
        raise ValueError("review_input_too_large")
    return ModelRequest(
        [ChatMessage("system", instructions), ChatMessage("user", encoded)],
        output,
        schema_name="sleepscape_sleep_review",
    )


def validate_output(value, evidence):
    try:
        _validator("output").validate(value)
    except ValidationError:
        raise ModelError("output_schema_mismatch") from None
    references = {
        item["id"]: item
        for key in ("aggregates", "comparisons", "nights")
        for item in evidence[key]
    }
    night_ids = {row["id"] for row in evidence["nights"]}
    statements = [value["summary"], *value["observations"], *value["hypotheses"], *value["actions"]]
    for item in statements:
        ids = set(item["evidence_ids"])
        if (
            not ids <= references.keys()
            or not (ids & night_ids) <= set(evidence["examples"])
            or any(
                not item[key].strip()
                or not report_text_is_safe(item[key])
                or _UNSUPPORTED.search(item[key])
                for key in ("text", "alternative", "observe")
                if key in item
            )
        ):
            raise ModelError("output_business_mismatch")
        if _COMPARATIVE.search(item["text"]) and not _comparable(
            [references[ref] for ref in ids], item["text"]
        ):
            raise ModelError("output_business_mismatch")
    # A comparative or explanatory assertion requires precomputed comparisons or
    # direct paired evidence, even when its other references exist.
    for item in value["hypotheses"]:
        ids = set(item["evidence_ids"])
        if (
            not any(ref.startswith("C") for ref in ids) and len(ids & night_ids) < 2
        ) or not _comparable([references[ref] for ref in ids], item["text"]):
            raise ModelError("output_business_mismatch")
    return value


def _comparable(rows, text):
    selected = {metric for term, metric in _ENVIRONMENT_TERMS.items() if term in text}
    if selected:
        return all(_metric_comparable(rows, metric) for metric in selected)
    return _metric_comparable(rows)


def _metric_comparable(rows, selected=None):
    if any(row["id"].startswith("C") and selected in (None, row["metric"]) for row in rows):
        return True
    nights = [row for row in rows if row["id"].startswith("N")]
    for aggregate in (row for row in rows if row["id"].startswith("A")):
        if selected in (None, aggregate["metric"]) and any(
            aggregate["metric"] in night["metrics"]
            and aggregate["environment_group"] in (None, night["environment_group"])
            and aggregate["valid_n"] > 1
            for night in nights
        ):
            return True
    if selected is None:
        return len(nights) > 1
    groups = Counter(night["environment_group"] for night in nights if selected in night["metrics"])
    return max(groups.values(), default=0) >= 2


def parse_output(raw, evidence):
    report = validate_output(strict_json(raw), evidence)
    summary = report["summary"]
    # Each comparison contrasts nights with poor vs good self-reported sleep.
    sleep_ids = set(evidence["examples"]) | {row["id"] for row in evidence["comparisons"]}
    sleep_ids.update(
        row["id"] for row in evidence["aggregates"] if row["metric"].startswith("sleep_")
    )
    if not sleep_ids.intersection(summary["evidence_ids"]) or opens_with_collection_status(
        summary["text"]
    ):
        raise ModelError("output_business_mismatch")
    return report
