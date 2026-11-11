"""Environment ingestion and offline device provisioning use cases."""

import json
import os
import secrets
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from sleepscape.config import Settings
from sleepscape.domain.environment import (
    MAX_BATCH_BYTES,
    EnvironmentBatch,
    Identifier,
    MeasurementProfile,
)
from sleepscape.storage import environment as storage


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("nonfinite_json_number")


def parse_batch(body: bytes) -> EnvironmentBatch:
    if len(body) > MAX_BATCH_BYTES:
        raise storage.EnvironmentError("environment_body_too_large", 413)
    try:
        payload = json.loads(
            body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
            raise ValueError("invalid_schema_version")
        return EnvironmentBatch.model_validate(payload)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise storage.EnvironmentError("invalid_environment_batch", 422) from None


def receive(settings: Settings, device_id: str, batch: EnvironmentBatch) -> dict:
    return storage.ingest(settings.data_dir, device_id, batch)


def read_profile(path: Path) -> MeasurementProfile:
    try:
        if not path.is_file() or path.stat().st_size > 8192:
            raise ValueError
        value = json.loads(
            path.read_bytes(), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
        return MeasurementProfile.model_validate(value)
    except (OSError, ValueError, TypeError, RecursionError):
        raise storage.EnvironmentError("invalid_measurement_profile", 422) from None


def identifiers(device_id: str, profile_id: str | None = None) -> None:
    try:
        adapter = TypeAdapter(Identifier)
        adapter.validate_python(device_id)
        if profile_id is not None:
            adapter.validate_python(profile_id)
    except ValidationError:
        raise storage.EnvironmentError("invalid_device_or_profile_id", 422) from None


def register_device(
    settings: Settings, device_id: str, profile_id: str, profile_path: Path, token_file: Path
) -> dict:
    """Write token once to a private user-selected file; never print it or put it in argv."""
    identifiers(device_id, profile_id)
    profile = read_profile(profile_path)
    if token_file.exists() or token_file.is_symlink() or token_file.parent.is_symlink():
        raise storage.EnvironmentError("device_token_path_not_new", 409)
    token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Refuse broad existing directories rather than changing unrelated directory permissions.
    if token_file.parent.stat().st_mode & 0o077:
        raise storage.EnvironmentError("device_token_directory_requires_private_permissions", 422)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"device_id": device_id, "profile_id": profile_id, "token": token}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        storage.register(settings.data_dir, device_id, profile_id, profile, token)
    except Exception:
        token_file.unlink(missing_ok=True)
        raise
    return {
        "status": "registered",
        "device_id": device_id,
        "profile_id": profile_id,
        "token_written_to_private_file": True,
        "ingest_enabled": settings.environment_ingest_enabled,
    }


def register_profile(
    settings: Settings, device_id: str, profile_id: str, profile_path: Path
) -> dict:
    identifiers(device_id, profile_id)
    storage.add_profile(settings.data_dir, device_id, profile_id, read_profile(profile_path))
    return {"status": "registered", "device_id": device_id, "profile_id": profile_id}


def rotate_device_token(settings: Settings, device_id: str, token_file: Path) -> dict:
    identifiers(device_id)
    if token_file.exists() or token_file.is_symlink() or token_file.parent.is_symlink():
        raise storage.EnvironmentError("device_token_path_not_new", 409)
    token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if token_file.parent.stat().st_mode & 0o077:
        raise storage.EnvironmentError("device_token_directory_requires_private_permissions", 422)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"device_id": device_id, "token": token}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        storage.rotate_token(settings.data_dir, device_id, token)
    except Exception:
        token_file.unlink(missing_ok=True)
        raise
    return {
        "status": "token_rotated",
        "device_id": device_id,
        "token_written_to_private_file": True,
    }
