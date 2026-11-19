"""Explicit synthetic model checks and read-only metadata, never sleep inference."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from jsonschema import Draft202012Validator

from sleepscape.config import Settings
from sleepscape.models.base import ChatMessage, ModelError, ModelRequest
from sleepscape.models.settings import (
    configuration_fingerprint,
    configuration_status,
    load_model_settings,
)
from sleepscape.services.model_call import call_model
from sleepscape.storage import model_runtime as runtime


def model_status(settings: Settings) -> dict:
    """No writes or external calls; malformed local state never disables Garmin."""
    now = datetime.now(UTC)
    configuration = configuration_status(settings)
    result = {
        **configuration,
        "connection_state": "not_checked",
        "runtime_state": "ready",
        "last_check_at": None,
        "last_check_status": None,
        "last_error_code": None,
        "usage": {},
        "budget": {"limit": runtime.DAILY_LIMIT, "used": 0, "remaining": runtime.DAILY_LIMIT},
    }
    try:
        state = runtime.read_state(settings.data_dir)
        result["budget"] = runtime.budget(state, now)
        if not state["attempts"]:
            return result
        row = state["attempts"][-1]
        status, code = row["status"], row["code"]
        if status == "running" and now - datetime.fromisoformat(row["started_at"]) > timedelta(
            seconds=185
        ):
            status, code = "failed", "model_worker_failed"
        result.update(
            last_check_at=row["finished_at"] or row["started_at"],
            last_check_status=status,
            last_error_code=code,
            usage=row["usage"],
        )
        if configuration_fingerprint(settings) != row["fingerprint"]:
            result["connection_state"] = "configuration_changed"
        elif status == "verified":
            result["connection_state"] = "verified"
        elif status == "failed":
            result["connection_state"] = "failed"
    except ModelError as exc:
        if exc.code == "model_state_unavailable":
            result.update(
                runtime_state="unavailable",
                connection_state="failed",
                last_error_code="model_state_unavailable",
                budget={"limit": runtime.DAILY_LIMIT, "used": None, "remaining": None},
            )
        else:
            result["connection_state"] = "configuration_changed"
    except Exception:
        result.update(
            runtime_state="unavailable",
            connection_state="failed",
            last_error_code="model_state_unavailable",
            budget={"limit": runtime.DAILY_LIMIT, "used": None, "remaining": None},
        )
    return result


def _request(nonce: str) -> ModelRequest:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ok", "nonce", "items", "note"],
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "nonce": {"type": "string", "const": nonce},
            "items": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
            "note": {"type": "string", "enum": ["synthetic"]},
        },
    }
    payload = {"ok": True, "nonce": nonce, "items": [1, 2], "note": "synthetic"}
    return ModelRequest(
        [
            ChatMessage("system", "This is a synthetic API capability check. Return only JSON."),
            ChatMessage("user", "Return exactly this JSON object: " + json.dumps(payload)),
        ],
        schema,
        schema_name="sleepscape_connection_check",
        max_completion_tokens=512,
    )


def run_model_check(settings: Settings) -> dict:
    """One explicit, budgeted synthetic call. Errors are data, never provider text."""
    attempt_id = None
    try:
        model = load_model_settings(settings)
        if not model.enabled:
            raise ModelError("model_disabled")
        # Includes safe credential-file stat checks, without reading its contents.
        configuration = configuration_status(settings)
        if not configuration["configured"]:
            raise ModelError(configuration.get("code") or "model_not_configured")
        fingerprint = configuration_fingerprint(settings)
        with runtime.session(settings.data_dir) as ledger:
            attempt_id = ledger.reserve(fingerprint, datetime.now(UTC))
            began = time.monotonic()
            usage = {}
            failure = None
            try:
                nonce = uuid4().hex
                request = _request(nonce)
                reply = call_model(model, request)
                usage = reply.usage
                value = json.loads(reply.text)
                if not Draft202012Validator(request.output_schema).is_valid(value):
                    raise ModelError("model_check_failed")
                if value != {"ok": True, "nonce": nonce, "items": [1, 2], "note": "synthetic"}:
                    raise ModelError("model_check_failed")
                if configuration_fingerprint(settings) != fingerprint:
                    raise ModelError("model_configuration_invalid")
            except ModelError as exc:
                failure = exc
            except (KeyboardInterrupt, SystemExit):
                ledger.finish(
                    attempt_id,
                    datetime.now(UTC),
                    int((time.monotonic() - began) * 1000),
                    error=ModelError("model_worker_failed"),
                    usage=usage,
                )
                raise
            except Exception:
                failure = ModelError("model_check_failed")
            ledger.finish(
                attempt_id,
                datetime.now(UTC),
                int((time.monotonic() - began) * 1000),
                error=failure,
                usage=usage,
            )
        status = model_status(settings)
        return {
            "status": "failed" if failure else "verified",
            "code": failure.code if failure else None,
            "synthetic": True,
            "attempted": True,
            "model": status,
        }
    except ModelError as exc:
        return {
            "status": "failed",
            "code": exc.code,
            "synthetic": True,
            "attempted": attempt_id is not None,
            "model": model_status(settings),
        }
