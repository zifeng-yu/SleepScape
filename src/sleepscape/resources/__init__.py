"""The single packaged report contract and Prompt, loaded without user data or network."""

import copy
import hashlib
import json
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

PROMPT_VERSION = "sleep-observation-7"


@lru_cache(maxsize=1)
def _contract():
    value = json.loads(files(__package__).joinpath("sleep-analysis.schema.json").read_text())
    Draft202012Validator.check_schema(value)
    return value


def contract_schema(kind):
    if kind not in {"input", "output", "model_output"}:
        raise ValueError("invalid_sleep_contract_kind")
    source = _contract()
    result = copy.deepcopy(source["$defs"][kind])
    result["$schema"] = source["$schema"]
    needed = set()

    def walk(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/$defs/"):
                key = ref.rsplit("/", 1)[1]
                if key not in needed:
                    needed.add(key)
                    walk(source["$defs"][key])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    result["$defs"] = {name: copy.deepcopy(source["$defs"][name]) for name in sorted(needed)}
    return result


@lru_cache(maxsize=3)
def _validator(kind):
    return Draft202012Validator(contract_schema(kind), format_checker=FormatChecker())


@lru_cache(maxsize=1)
def system_prompt():
    return files(__package__).joinpath("sleep-analysis-system.txt").read_text(encoding="utf-8")


def prompt_metadata():
    return {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(system_prompt().encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(
            json.dumps(
                _contract(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest(),
    }


def validate_sleep_input(payload):
    from sleepscape.domain.sleep_features import SleepAnalysisError, validate_input_semantics

    try:
        _validator("input").validate(payload)
        validate_input_semantics(payload)
    except SleepAnalysisError:
        raise
    except (ValidationError, ValueError, TypeError, KeyError, RecursionError):
        raise SleepAnalysisError("invalid_analysis_input") from None


def validate_sleep_output(payload, input_payload):
    from sleepscape.domain.sleep_features import SleepAnalysisError, validate_output_semantics

    validate_sleep_input(input_payload)
    try:
        _validator("output").validate(payload)
        validate_output_semantics(payload, input_payload)
    except SleepAnalysisError:
        raise
    except (ValidationError, ValueError, TypeError, KeyError, RecursionError):
        raise SleepAnalysisError("invalid_analysis_output") from None


def validate_model_output(payload, input_payload):
    """Validate the small model answer and return the complete stored report."""
    from sleepscape.domain.sleep_features import SleepAnalysisError, assemble_model_output

    validate_sleep_input(input_payload)
    try:
        _validator("model_output").validate(payload)
        report = assemble_model_output(payload, input_payload)
        validate_sleep_output(report, input_payload)
        return report
    except SleepAnalysisError:
        raise
    except (ValidationError, ValueError, TypeError, KeyError, RecursionError):
        raise SleepAnalysisError("invalid_analysis_output") from None
