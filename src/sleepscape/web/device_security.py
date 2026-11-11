"""Independent device authentication for one exact HTTPS POST endpoint."""

import re
import time
from collections import OrderedDict
from threading import Lock

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from sleepscape.config import Settings
from sleepscape.storage.environment import EnvironmentError, authenticate

INGEST_PATH = "/api/v1/ingest/environment-batches"


class DeviceGate:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = Lock()

    def _allowed(self, peer: str) -> bool:
        # One device can recover 48h in bounded batches without opening an unbounded write loop.
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(peer, (20.0, now))
            tokens = min(20.0, tokens + (now - last) * 2)
            allowed = tokens >= 1
            self._buckets[peer] = (tokens - 1 if allowed else tokens, now)
            self._buckets.move_to_end(peer)
            while len(self._buckets) > 128:
                self._buckets.popitem(last=False)
            return allowed

    async def check(self, request):
        if not self.settings.environment_ingest_enabled:
            return JSONResponse({"error": "environment_ingest_disabled"}, status_code=405)
        if request.scope.get("raw_path", b"") != INGEST_PATH.encode():
            return JSONResponse({"error": "invalid_ingest_path"}, status_code=404)
        host = request.url.hostname
        if host not in self.settings.web_allowed_hosts and not (
            host == "::1" and "[::1]" in self.settings.web_allowed_hosts
        ):
            return JSONResponse({"error": "host_not_allowed"}, status_code=400)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "cross_site_request_blocked"}, status_code=403)
        # Only ASGI's scheme is authoritative; trusted proxy handling belongs to Uvicorn.
        if request.url.scheme != "https":
            return JSONResponse({"error": "https_required"}, status_code=403)
        peer = request.client.host if request.client else "unknown"
        if not self._allowed(peer):
            return JSONResponse(
                {"error": "environment_rate_limited"}, status_code=429, headers={"Retry-After": "1"}
            )
        if request.headers.get("content-encoding", "identity") != "identity":
            return JSONResponse(
                {"error": "compressed_environment_body_not_supported"}, status_code=415
            )
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse({"error": "environment_json_required"}, status_code=415)
        header = request.headers.get("authorization", "")
        if not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43,128}", header):
            return JSONResponse({"error": "device_authentication_required"}, status_code=401)
        try:
            device_id = await run_in_threadpool(authenticate, self.settings.data_dir, header[7:])
        except EnvironmentError:
            return JSONResponse({"error": "environment_not_initialized"}, status_code=503)
        except Exception:
            return JSONResponse({"error": "environment_auth_unavailable"}, status_code=503)
        if device_id is None:
            return JSONResponse({"error": "device_authentication_required"}, status_code=401)
        request.state.environment_device_id = device_id
        return None
