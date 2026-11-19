"""Read public model settings and inspect credentials without exposing their contents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sleepscape.config import Settings
from sleepscape.models.base import ModelError
from sleepscape.models.schema_transport import SCHEMA_PROFILES

DEFAULT_CONFIG = Path("configs/models.toml")
MAX_KEY_BYTES = 4096
MAX_CONFIG_BYTES = 16384
RESPONSE_MODES = frozenset({"json_schema", "json_object"})
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
_REF = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class ModelSettings:
    model_ref: str
    provider: str
    model: str
    endpoint: str = field(repr=False)
    key_file: Path = field(repr=False)
    enabled: bool
    timeout_seconds: int
    max_completion_tokens: int
    enable_thinking: bool
    accepted_models: tuple[str, ...]
    schema_profile: str = "native"
    response_mode: str = "json_schema"


def _endpoint(base: object, endpoint: object) -> str:
    if base and endpoint:
        raise ModelError("model_configuration_invalid")
    raw = endpoint or base
    if raw is None or raw == "":
        raise ModelError("model_not_configured")
    if not isinstance(raw, str) or len(raw) > 2048 or raw != raw.strip():
        raise ModelError("model_configuration_invalid")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ModelError("model_configuration_invalid")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
        decoded_path = unquote(parsed.path)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "?" in raw
            or "#" in raw
            or "\\" in raw
            or any(part in {".", ".."} for part in decoded_path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded_path)
        ):
            raise ValueError
        if not parsed.hostname.isascii():
            raise ValueError
    except ValueError:
        raise ModelError("model_configuration_invalid") from None
    cleaned = raw.rstrip("/")
    if endpoint:
        if not cleaned.endswith("/chat/completions"):
            raise ModelError("model_configuration_invalid")
        return cleaned
    if cleaned.endswith("/chat/completions"):
        raise ModelError("model_configuration_invalid")
    return cleaned + "/chat/completions"


def _public_configuration(settings: Settings) -> dict:
    try:
        path = Path(settings.llm_config_file)
        if path == DEFAULT_CONFIG and not path.exists():
            path = Path(__file__).with_name("defaults.toml")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError
        raw = path.read_bytes()
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError
        document = tomllib.loads(raw.decode("utf-8"))
        ref = settings.llm_model_ref
        if not isinstance(ref, str) or _REF.fullmatch(ref) is None:
            raise ValueError
        if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
            raise ValueError
        value = document["models"][ref]
        allowed = {
            "provider",
            "model",
            "accepted_models",
            "timeout_seconds",
            "max_completion_tokens",
            "enable_thinking",
            "schema_profile",
            "response_mode",
        }
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError
        model, provider = value["model"], value["provider"]
        if (
            provider != "openai_compatible"
            or not isinstance(model, str)
            or not _NAME.fullmatch(model)
        ):
            raise ValueError
        timeout, tokens = value["timeout_seconds"], value["max_completion_tokens"]
        if type(timeout) is not int or not 1 <= timeout <= 180:
            raise ValueError
        if type(tokens) is not int or not 1 <= tokens <= 4096:
            raise ValueError
        thinking = value["enable_thinking"]
        if type(thinking) is not bool:
            raise ValueError
        profile = value.get("schema_profile", "native")
        if not isinstance(profile, str) or profile not in SCHEMA_PROFILES:
            raise ValueError
        response_mode = value.get("response_mode", "json_schema")
        if not isinstance(response_mode, str) or response_mode not in RESPONSE_MODES:
            raise ValueError
        accepted = value.get("accepted_models", [model])
        if (
            not isinstance(accepted, list)
            or not 1 <= len(accepted) <= 16
            or model not in accepted
            or any(not isinstance(item, str) or not _NAME.fullmatch(item) for item in accepted)
            or len(set(accepted)) != len(accepted)
        ):
            raise ValueError
        return {
            **value,
            "accepted_models": tuple(accepted),
            "model_ref": ref,
            "schema_profile": profile,
            "response_mode": response_mode,
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise ModelError("model_configuration_invalid") from None


def key_metadata(path: Path) -> tuple[int, int, int, int, int]:
    """Stat only. GET/doctor must not read credential bytes or create any file."""
    try:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ModelError("model_key_permissions")
        parent = path.parent.stat()
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not stat.S_ISDIR(parent.st_mode):
            raise ModelError("model_key_permissions")
        if stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ModelError("model_key_permissions")
        if hasattr(os, "getuid") and (info.st_uid != os.getuid() or parent.st_uid != os.getuid()):
            raise ModelError("model_key_permissions")
        if not 0 < info.st_size <= MAX_KEY_BYTES:
            raise ModelError("model_key_invalid")
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except FileNotFoundError:
        raise ModelError("model_key_missing") from None
    except OSError:
        raise ModelError("model_key_permissions") from None


def read_api_key(path: Path) -> str:
    """Only the network adapter calls this; never return credential material to audit."""
    before = key_metadata(path)
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        opened = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        if before != opened or stat.S_IMODE(info.st_mode) != 0o600:
            raise ModelError("model_key_permissions")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(MAX_KEY_BYTES + 1)
        if len(raw) > MAX_KEY_BYTES:
            raise ModelError("model_key_invalid")
        # A final newline from conventional private-file provisioning is allowed;
        # internal newlines or other header-control bytes are not.
        key = raw.decode("ascii").removesuffix("\n").removesuffix("\r")
        if not key or key != key.strip() or any(not 33 <= ord(char) <= 126 for char in key):
            raise ModelError("model_key_invalid")
        return key
    except ModelError:
        raise
    except (OSError, UnicodeError):
        raise ModelError("model_key_invalid") from None
    finally:
        if fd is not None:
            os.close(fd)


def load_model_settings(settings: Settings) -> ModelSettings:
    value = _public_configuration(settings)
    endpoint = _endpoint(settings.llm_base_url, settings.llm_endpoint)
    key_file = Path(settings.llm_key_file)
    key_metadata(key_file)
    return ModelSettings(
        model_ref=value["model_ref"],
        provider=value["provider"],
        model=value["model"],
        endpoint=endpoint,
        key_file=key_file,
        enabled=settings.llm_enabled,
        timeout_seconds=value["timeout_seconds"],
        max_completion_tokens=value["max_completion_tokens"],
        enable_thinking=value["enable_thinking"],
        accepted_models=value["accepted_models"],
        schema_profile=value["schema_profile"],
        response_mode=value["response_mode"],
    )


def configuration_status(settings: Settings) -> dict:
    """Public whitelist; configuration completeness is not network verification."""
    result = {
        "configured": False,
        "enabled": settings.llm_enabled is True,
        "model_ref": "sleep_analyst",
        "model": None,
        "provider": None,
        "configuration_state": "not_configured",
        "code": None,
    }
    try:
        value = _public_configuration(settings)
        result.update(
            model_ref=value["model_ref"], model=value["model"], provider=value["provider"]
        )
        model = load_model_settings(settings)
        result.update(
            configured=True,
            configuration_state="configured" if model.enabled else "disabled",
            code=None if model.enabled else "model_disabled",
        )
    except ModelError as exc:
        result["code"] = exc.code
        if exc.code not in {"model_not_configured", "model_key_missing"}:
            result["configuration_state"] = "invalid_configuration"
    return result


def configuration_fingerprint(settings: Settings) -> str:
    """Private stable binding; key replacement changes metadata, not public status secrets."""
    model = load_model_settings(settings)
    values = {
        "version": 1,
        "model_ref": model.model_ref,
        "provider": model.provider,
        "model": model.model,
        "endpoint": model.endpoint,
        "key_file": str(model.key_file.absolute()),
        "key_metadata": key_metadata(model.key_file),
        "enabled": model.enabled,
        "timeout_seconds": model.timeout_seconds,
        "max_completion_tokens": model.max_completion_tokens,
        "enable_thinking": model.enable_thinking,
        "accepted_models": model.accepted_models,
        "schema_profile": model.schema_profile,
        "response_mode": model.response_mode,
    }
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
