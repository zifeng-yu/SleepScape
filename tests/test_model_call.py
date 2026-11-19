"""Spawn real local children with synthetic workers; never make network requests."""

import json
import multiprocessing
import os
import struct
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sleepscape.models.base import ChatMessage, ModelError, ModelRequest, ModelResult
from sleepscape.models.settings import ModelSettings
from sleepscape.services import model_call as calls


def model_settings():
    return ModelSettings(
        model_ref="sleep_analyst",
        provider="openai_compatible",
        model="qwen3.8-max",
        endpoint="https://example.invalid/v1/chat/completions",
        key_file=Path("never-open-this-test-key"),
        enabled=True,
        timeout_seconds=180,
        max_completion_tokens=4096,
        enable_thinking=False,
        accepted_models=("qwen3.8-max",),
    )


def request():
    return ModelRequest([ChatMessage("user", "synthetic")], {"type": "object"})


def large_worker(connection, settings, request):
    packet = {
        "ok": True,
        "text": "x" * 200_000,
        "model": "qwen3.8-max",
        "finish_reason": "stop",
        "usage": {"total_tokens": 3, "raw": "secret-not-exported"},
    }
    connection.send_bytes(json.dumps(packet).encode())
    connection.close()


def timeout_worker(connection, settings, request):
    time.sleep(20)


def partial_frame_worker(connection, settings, request):
    os.write(connection.fileno(), struct.pack("!i", 100))
    os.write(connection.fileno(), b"{")
    time.sleep(20)


def exit_worker(connection, settings, request):
    os._exit(7)


def oversize_worker(connection, settings, request):
    connection.send_bytes(b"x" * (calls.MAX_IPC_BYTES + 1))


def error_worker(connection, settings, request):
    connection.send_bytes(
        json.dumps(
            {
                "ok": False,
                "code": "rate_limited",
                "retryable": True,
                "http_status": 429,
                "retry_after": 60,
                "message": "provider-secret-body",
            }
        ).encode()
    )


@pytest.mark.parametrize("worker", [timeout_worker, partial_frame_worker])
def test_hard_deadline_reclaims_worker_even_partial_pipe_frame(worker):
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(ModelError, match="model_deadline_exceeded"):
        calls._execute(model_settings(), request(), 0.8, worker=worker)
    assert time.monotonic() - started < 2
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_large_result_is_drained_before_joining():
    result = calls._execute(model_settings(), request(), 5, worker=large_worker)
    assert len(result.text) == 200_000
    assert result.usage == {"total_tokens": 3}
    assert result.text not in repr(result)


@pytest.mark.parametrize("worker", [exit_worker, oversize_worker])
def test_worker_failures_are_bounded_and_reclaimed(worker):
    with pytest.raises(ModelError, match="model_worker_failed"):
        calls._execute(model_settings(), request(), 5, worker=worker)


def test_error_packet_whitelist():
    with pytest.raises(ModelError) as caught:
        calls._execute(model_settings(), request(), 5, worker=error_worker)
    error = caught.value
    assert error.code == "rate_limited"
    assert error.http_status == 429
    assert error.retry_after == 60
    assert error.retryable
    assert "provider-secret-body" not in repr(error)


@pytest.mark.parametrize("timeout", [0, -1, True, "1", float("inf"), float("nan")])
def test_invalid_deadline_cannot_launch_worker(timeout, monkeypatch):
    monkeypatch.setattr(calls, "_execute", lambda *_: pytest.fail("No worker may start"))
    with pytest.raises(ModelError, match="invalid_model_request"):
        calls.call_model(model_settings(), request(), timeout_seconds=timeout)


def test_public_deadline_cannot_exceed_settings_or_180(monkeypatch):
    observed = []
    monkeypatch.setattr(calls, "_execute", lambda *args: observed.append(args[-1]))
    calls.call_model(model_settings(), request(), timeout_seconds=200)
    calls.call_model(replace(model_settings(), timeout_seconds=5), request(), timeout_seconds=10)
    assert observed == [180.0, 5.0]


@pytest.mark.parametrize("raw", [b"{", b"[]", b'{"ok": true}', b'{"ok": "true"}'])
def test_malformed_ipc_is_sanitized(raw):
    with pytest.raises(ModelError, match="model_worker_failed"):
        calls._decode(raw, model_settings())


@pytest.mark.parametrize(
    "normalization", [None, "none", "json_string_envelope", "single_object_array"]
)
def test_normalization_ipc_is_optional_and_backward_compatible(normalization):
    packet = {
        "ok": True,
        "text": "{}",
        "model": "qwen3.8-max",
        "finish_reason": "stop",
        "usage": {},
    }
    if normalization is not None:
        packet["transport_normalization"] = normalization
    result = calls._decode(json.dumps(packet).encode(), model_settings())
    assert result.transport_normalization == (normalization or "none")


@pytest.mark.parametrize("normalization", ["SYNTHETIC_PRIVATE", "", None, True, []])
def test_normalization_ipc_rejects_unknown_values(normalization):
    packet = {
        "ok": True,
        "text": "{}",
        "model": "qwen3.8-max",
        "finish_reason": "stop",
        "usage": {},
        "transport_normalization": normalization,
    }
    with pytest.raises(ModelError) as caught:
        calls._decode(json.dumps(packet).encode(), model_settings())
    assert str(caught.value) == "model_worker_failed"
    assert "SYNTHETIC_PRIVATE" not in repr(caught.value)


@pytest.mark.parametrize(
    "normalization", ["none", "json_string_envelope", "single_object_array", "SYNTHETIC_PRIVATE"]
)
def test_worker_exports_only_registered_nondefault_normalization(monkeypatch, normalization):
    sent = []
    result = ModelResult("{}", "qwen3.8-max", "stop", transport_normalization=normalization)
    monkeypatch.setattr(
        calls, "create_client", lambda _: SimpleNamespace(complete=lambda _: result)
    )
    monkeypatch.setattr(calls.logging, "disable", lambda _: None)
    connection = SimpleNamespace(send_bytes=sent.append, close=lambda: None)
    calls._worker(connection, model_settings(), request())
    assert len(sent) == 1
    packet = json.loads(sent[0])
    if normalization == "none":
        assert packet["ok"] is True and "transport_normalization" not in packet
    elif normalization in {"json_string_envelope", "single_object_array"}:
        assert packet["transport_normalization"] == normalization
        assert calls._decode(sent[0], model_settings()).transport_normalization == normalization
    else:
        assert packet["ok"] is False and packet["code"] == "invalid_response"
        assert b"SYNTHETIC_PRIVATE" not in sent[0]
