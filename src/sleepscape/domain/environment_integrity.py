"""Pure integrity checks for persisted environment payloads and their indexed columns."""

import hashlib
import json

from pydantic import TypeAdapter

from sleepscape.domain.environment import (
    DeviceStatus,
    MeasurementProfile,
    Record,
    TimeAnchor,
    canonical_json,
)


def validate_environment_row(table: str, row: dict) -> None:
    text = row["payload_json"]
    if len(text.encode()) > 65_536:
        raise ValueError("environment_payload_too_large")
    if hashlib.sha256(text.encode()).hexdigest() != row["content_hash"]:
        raise ValueError("environment_payload_hash_mismatch")
    value = json.loads(text)
    if canonical_json(value) != text:
        raise ValueError("environment_payload_not_canonical")
    if table == "environment_profiles":
        MeasurementProfile.model_validate(value)
        return
    if table == "environment_records":
        item = TypeAdapter(Record).validate_python(value)
        checks = {
            "record_type": item.record_type,
            "seq": item.seq,
            "profile_id": item.measurement_profile_id,
            "mono_start_ms": item.mono_start_ms,
            "mono_end_ms": item.mono_end_ms,
        }
    elif table == "environment_time_anchors":
        item = TimeAnchor.model_validate(value)
        checks = {
            "anchor_id": item.anchor_id,
            "monotonic_ms": item.monotonic_ms,
            "utc_at": item.utc_at,
        }
    elif table == "environment_device_status":
        item = DeviceStatus.model_validate(value)
        checks = {"status_seq": item.status_seq, "monotonic_ms": item.monotonic_ms}
    else:
        raise ValueError("unknown_environment_table")
    if any(row[key] != expected for key, expected in checks.items()):
        raise ValueError("environment_index_mismatch")
