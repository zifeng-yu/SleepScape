"""Constrained official SDK transport with safe errors and bounded retries."""

import logging
import math
import stat
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator
from garmin_personal_api.errors import HttpError

from sleepscape.adapters.catalog import ALLOWED_GET_PATHS, TOKEN_PATH
from sleepscape.config import Settings
from sleepscape.domain.models import DataRequest, FetchResult

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
_NETWORK_ERRORS = (
    requests.Timeout,
    requests.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


class GarminError(Exception):
    """Only static codes, status numbers and retry timing are safe to expose."""

    def __init__(
        self,
        code: str,
        http_status: int | None = None,
        *,
        retry_after_seconds: float | None = None,
    ):
        self.code = code
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)
        # SDK HttpError contains its response body. Nested retry/authentication
        # helpers must not accidentally expose it through exception chaining.
        self.__suppress_context__ = True


class ScopedSession(requests.Session):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.last_status: int | None = None
        self.retry_after: str | None = None
        self.request_count = 0

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        token_call = method.upper() == "POST" and parsed.path == TOKEN_PATH
        data_call = method.upper() == "GET" and any(
            pattern.fullmatch(parsed.path) for pattern in ALLOWED_GET_PATHS
        )
        if (
            origin != self.base_url
            or parsed.query
            or parsed.fragment
            or not (token_call or data_call)
        ):
            raise GarminError("request_outside_allowed_scope")
        kwargs["allow_redirects"] = False
        kwargs["stream"] = True
        self.last_status = None
        self.retry_after = None
        self.request_count += 1
        response = super().request(method, url, **kwargs)
        self.last_status = response.status_code
        self.retry_after = response.headers.get("Retry-After")
        try:
            if 300 <= response.status_code < 400:
                raise GarminError("redirect_blocked", response.status_code)
            try:
                declared_size = int(response.headers.get("Content-Length", "0"))
            except ValueError:
                declared_size = 0
            if declared_size > MAX_RESPONSE_BYTES:
                raise GarminError("response_too_large", response.status_code)
            chunks: list[bytes] = []
            size = 0
            # iter_content counts decoded bytes too, bounding compressed bodies.
            for chunk in response.iter_content(chunk_size=64 * 1024):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise GarminError("response_too_large", response.status_code)
                chunks.append(chunk)
            # Preserve the SDK's response.content/text/json interface after close.
            response._content = b"".join(chunks)
            response._content_consumed = True
            return response
        finally:
            response.close()


def _private_file(path: Path) -> Path:
    if path.is_symlink() or path.parent.is_symlink():
        raise GarminError("credential_symlink_not_allowed")
    if path.exists():
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise GarminError("credential_file_permissions")
    if not path.parent.is_dir() or stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise GarminError("credential_directory_permissions")
    return path


class GarminGateway:
    def __init__(self, settings: Settings, *, sleeper=time.sleep):
        self.settings = settings
        self.sleeper = sleeper
        self.session = ScopedSession(settings.garmin_base_url)
        cache_file = _private_file(settings.garmin_token_cache)
        self.auth = PersonalApiAuthenticator(
            timeout=20,
            session=self.session,
            bearer_token_cache_file=cache_file,
        )
        # The SDK reads process environment in its constructor, not our .env file.
        # Set the public instance URL explicitly; ScopedSession also enforces it.
        self.auth.base_url = settings.garmin_base_url
        self.client: GarminPersonalApiClient | None = None
        self.auth_refreshes = 0
        logging.getLogger("garmin_personal_api").setLevel(logging.WARNING)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.session.close()

    def authenticate(self, *, force: bool = False):
        try:
            key_file = _private_file(self.settings.garmin_key_file)
            key = key_file.read_text(encoding="utf-8").strip()
            if not key:
                raise GarminError("empty_key_file")
            for attempt in range(3):
                try:
                    token = self.auth.obtain_token(key, force_refresh=force)
                    break
                except HttpError as exc:
                    status = exc.status_code
                    if status not in _TRANSIENT_STATUSES:
                        raise GarminError("authentication_rejected", status) from None
                    delay = self._retry_delay(attempt)
                    if attempt == 2:
                        raise GarminError(
                            "authentication_retry_exhausted",
                            status,
                            retry_after_seconds=delay if self.session.retry_after else None,
                        ) from None
                    self.sleeper(delay)
                except _NETWORK_ERRORS:
                    if attempt == 2:
                        raise GarminError("authentication_network_retry_exhausted") from None
                    self.sleeper(float(2**attempt))
            if token.expires_at <= time.time():
                raise GarminError("expired_token")
            self.settings.garmin_token_cache.chmod(0o600)
            if self.client is None:
                self.client = GarminPersonalApiClient(
                    token.access_token,
                    timeout=20,
                    session=self.session,
                )
                self.client.base_url = self.settings.garmin_base_url
            else:
                self.client.update_bearer_token(token.access_token)
        except GarminError:
            raise
        except requests.RequestException:
            raise GarminError("authentication_network_error") from None
        except OSError:
            raise GarminError("credential_file_unavailable") from None
        except Exception:
            raise GarminError("authentication_invalid_response") from None

    def _retry_delay(self, attempt: int) -> float:
        value = self.session.retry_after
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 2**attempt
            if not math.isfinite(delay):
                delay = 2**attempt
            if delay > 10:
                raise GarminError(
                    "retry_after_deferred",
                    self.session.last_status,
                    retry_after_seconds=delay,
                )
            return max(0.0, delay)
        return float(2**attempt)

    def fetch(self, request: DataRequest) -> FetchResult:
        if self.client is None:
            self.authenticate()
        transient_failures = 0
        while True:
            self.sleeper(0.2)
            try:
                assert self.client is not None
                payload = self.client.get(request.path, params=request.params)
                return FetchResult(
                    request,
                    payload,
                    datetime.now(UTC).isoformat(),
                    self.session.last_status or 200,
                )
            except HttpError as exc:
                status = exc.status_code
                if status == 401 and self.auth_refreshes == 0:
                    self.auth_refreshes += 1
                    self.authenticate(force=True)
                    continue
                if status in {401, 403}:
                    raise GarminError("authorization_failed", status) from None
                if status not in _TRANSIENT_STATUSES:
                    raise GarminError("http_error", status) from None
                delay = self._retry_delay(transient_failures)
                transient_failures += 1
                if transient_failures == 3:
                    raise GarminError(
                        "retry_exhausted",
                        status,
                        retry_after_seconds=delay if self.session.retry_after else None,
                    ) from None
                self.sleeper(delay)
            except requests.exceptions.JSONDecodeError:
                raise GarminError("invalid_json_response", self.session.last_status) from None
            except _NETWORK_ERRORS:
                transient_failures += 1
                if transient_failures == 3:
                    raise GarminError("network_retry_exhausted") from None
                self.sleeper(float(2 ** (transient_failures - 1)))
            except GarminError:
                raise
            except Exception:
                raise GarminError("invalid_api_response", self.session.last_status) from None
