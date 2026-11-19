"""Reclaimable, bounded model subprocess; no database or long-lived background worker."""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import os
import queue
import threading
import time
from contextlib import redirect_stderr, redirect_stdout

from sleepscape.models.audit import error_metadata, safe_usage
from sleepscape.models.base import TRANSPORT_NORMALIZATIONS, ModelError, ModelRequest, ModelResult
from sleepscape.models.factory import create_client
from sleepscape.models.settings import ModelSettings

MAX_IPC_BYTES = 300 * 1024


def _worker(connection, settings: ModelSettings, request: ModelRequest) -> None:
    """Only a bounded JSON packet crosses IPC; provider exceptions are never printed."""
    logging.disable(logging.CRITICAL)
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            try:
                result = create_client(settings).complete(request)
                normalization = result.transport_normalization
                if (
                    not isinstance(normalization, str)
                    or normalization not in TRANSPORT_NORMALIZATIONS
                ):
                    raise ModelError("invalid_response")
                packet = {
                    "ok": True,
                    "text": result.text,
                    "model": result.model,
                    "finish_reason": result.finish_reason,
                    "usage": safe_usage(result.usage),
                }
                if normalization != "none":
                    packet["transport_normalization"] = normalization
            except ModelError as exc:
                packet = {"ok": False, **error_metadata(exc)}
            except BaseException:
                packet = {"ok": False, "code": "model_worker_failed"}
            raw = json.dumps(packet, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > MAX_IPC_BYTES:
                raw = b'{"ok":false,"code":"response_too_large"}'
            connection.send_bytes(raw)
    except BaseException:
        # Parent observes EOF/process exit and returns a safe error.
        pass
    finally:
        connection.close()


def _decode(raw: bytes, settings: ModelSettings) -> ModelResult:
    try:
        packet = json.loads(raw)
        if not isinstance(packet, dict) or type(packet.get("ok")) is not bool:
            raise ValueError
        if not packet["ok"]:
            raise ModelError(
                packet.get("code"),
                retryable=packet.get("retryable") is True,
                http_status=packet.get("http_status"),
                retry_after=packet.get("retry_after"),
            )
        normalization = packet.get("transport_normalization", "none")
        if (
            set(packet) - {"transport_normalization"}
            != {"ok", "text", "model", "finish_reason", "usage"}
            or not isinstance(normalization, str)
            or normalization not in TRANSPORT_NORMALIZATIONS
            or not isinstance(packet["text"], str)
            or not packet["text"].strip()
            or len(packet["text"].encode()) > 256 * 1024
            or packet["model"] not in settings.accepted_models
            or packet["finish_reason"] != "stop"
            or not isinstance(packet["usage"], dict)
        ):
            raise ValueError
        return ModelResult(
            packet["text"],
            packet["model"],
            "stop",
            safe_usage(packet["usage"]),
            transport_normalization=normalization,
        )
    except ModelError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ModelError("model_worker_failed") from None


def _execute(settings, request, timeout, *, worker=_worker) -> ModelResult:
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(send, settings, request), daemon=True)
    started = False
    reader = None
    packets = queue.Queue(maxsize=1)
    # Leave time to reclaim the worker within the public hard deadline.
    deadline = time.monotonic() + timeout - min(0.5, timeout / 10)

    def read_packet():
        try:
            packets.put(receive.recv_bytes(MAX_IPC_BYTES))
        except (OSError, ValueError, EOFError):
            packets.put(None)

    try:
        process.start()
        started = True
        send.close()
        # A partial pipe frame can block recv_bytes even after poll() succeeds.
        # Keep that bounded reader off the deadline supervisor thread.
        reader = threading.Thread(target=read_packet, daemon=True, name="model-packet")
        reader.start()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelError("model_deadline_exceeded", retryable=True)
            try:
                raw = packets.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if raw is None:
                raise ModelError("model_worker_failed")
            if time.monotonic() >= deadline:
                raise ModelError("model_deadline_exceeded", retryable=True)
            return _decode(raw, settings)
    except ModelError:
        raise
    except (OSError, ValueError, EOFError):
        raise ModelError("model_worker_failed") from None
    finally:
        send.close()
        if started:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.15)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.15)
            if not process.is_alive():
                process.close()
        if reader is not None:
            reader.join(timeout=0.1)
        receive.close()


def call_model(
    settings: ModelSettings, request: ModelRequest, *, timeout_seconds: float | None = None
) -> ModelResult:
    """One physical request at most; callers must reserve a durable budget first."""
    timeout = settings.timeout_seconds if timeout_seconds is None else timeout_seconds
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ModelError("invalid_model_request")
    timeout = min(float(timeout), float(settings.timeout_seconds), 180.0)
    return _execute(settings, request, timeout)
