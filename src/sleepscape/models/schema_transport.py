"""Explicit provider schema projections; the original remains the local validator.

Only schema keyword positions are transformed. Property/definition names and
instance data in const/default/enum/examples are never treated as keywords.
"""

import copy
from urllib.parse import unquote

from sleepscape.models.base import ModelError

SCHEMA_PROFILES = frozenset({"native", "aliyun-compatible-1"})
_ALIYUN_UNSUPPORTED = frozenset(
    {
        "uniqueItems",
        "contains",
        "minContains",
        "maxContains",
        "additionalItems",
        "multipleOf",
        "patternProperties",
        "propertyNames",
    }
)
_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions", "dependentSchemas"})
_SCHEMA_ARRAYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_VALUES = frozenset(
    {
        "items",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)


def transport_schema(schema: dict, profile: str) -> dict:
    """Project transport constraints only; never mutate or replace the source contract."""
    if not isinstance(profile, str) or profile not in SCHEMA_PROFILES:
        raise ModelError("model_configuration_invalid")
    if profile == "native":
        return copy.deepcopy(schema)
    references = []

    def visit(value):
        if isinstance(value, bool):
            return value
        if not isinstance(value, dict):
            raise ModelError("invalid_model_request")
        result = {}
        for key, item in value.items():
            if key in _ALIYUN_UNSUPPORTED:
                continue
            if key in _SCHEMA_MAPS:
                result[key] = {name: visit(child) for name, child in item.items()}
            elif key in _SCHEMA_ARRAYS:
                result[key] = [visit(child) for child in item]
            elif key in _SCHEMA_VALUES:
                result[key] = (
                    [visit(child) for child in item] if isinstance(item, list) else visit(item)
                )
            elif key == "dependencies":
                result[key] = {
                    name: copy.deepcopy(child) if isinstance(child, list) else visit(child)
                    for name, child in item.items()
                }
            else:
                result[key] = copy.deepcopy(item)
                if key in {"$ref", "$dynamicRef"}:
                    references.append(item)
        return result

    result = visit(schema)
    # A removed constraint may itself have been a reference target. Do not send
    # an unresolved provider schema or silently redirect that reference.
    for reference in references:
        try:
            if not isinstance(reference, str) or not reference.startswith("#/"):
                raise ValueError
            target = result
            for part in unquote(reference[2:]).split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[int(part)] if isinstance(target, list) else target[part]
        except (ValueError, TypeError, KeyError, IndexError):
            raise ModelError("capability_unavailable") from None
    return result
