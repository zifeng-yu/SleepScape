"""Synthetic transport/authentication tests; no real Garmin account or network."""

import io
import time
import traceback
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from garmin_personal_api.errors import HttpError

from sleepscape.adapters.catalog import PATHS, TOKEN_PATH, daily_request
from sleepscape.adapters.garmin import MAX_RESPONSE_BYTES, GarminError, GarminGateway, ScopedSession
from sleepscape.config import Settings

BASE_URL = "https://personal.connectapi.garmin.cn"
SYNTHETIC_SECRET = "SYNTHETIC_PRIVATE_RESPONSE_MUST_NOT_APPEAR"
REQUEST = daily_request("sleep", date(2026, 11, 1))


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Tests must not send network requests")

    monkeypatch.setattr(requests.Session, "send", fail)


class TrackingResponse(requests.Response):
    def __init__(self, body=b"{}", *, status=200, headers=None):
        super().__init__()
        self.status_code = status
        self.headers.update(headers or {})
        self.raw = io.BytesIO(body)
        self.was_closed = False

    def close(self):
        self.was_closed = True
        super().close()
        self.raw.close()


def http_error(status):
    return HttpError(
        status_code=status,
        method="GET",
        url=BASE_URL + PATHS["sleep"],
        response_text=SYNTHETIC_SECRET,
    )


@pytest.fixture
def gateway(tmp_path):
    private = tmp_path / "synthetic-credentials"
    private.mkdir(mode=0o700)
    key_file = private / "key"
    key_file.write_text("SYNTHETIC_API_KEY", encoding="utf-8")
    key_file.chmod(0o600)
    token_file = private / "token.json"
    token_file.write_text("{}", encoding="utf-8")
    token_file.chmod(0o600)
    settings = Settings(
        _env_file=None,
        garmin_key_file=key_file,
        garmin_token_cache=token_file,
        GARMIN_PERSONAL_API_BASE_URL=BASE_URL,
    )
    with GarminGateway(settings, sleeper=Mock()) as result:
        yield result


def ready_client(gateway, outcomes, *, retry_after=None):
    iterator = iter(outcomes)

    def get(*args, **kwargs):
        outcome = next(iterator)
        gateway.session.retry_after = retry_after
        gateway.session.last_status = outcome.status_code if isinstance(outcome, HttpError) else 200
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    gateway.client = SimpleNamespace(get=Mock(side_effect=get), update_bearer_token=Mock())
    return gateway.client


def fake_auth(gateway, outcomes, *, retry_after=None):
    iterator = iter(outcomes)

    def obtain(*args, **kwargs):
        outcome = next(iterator)
        gateway.session.retry_after = retry_after
        gateway.session.last_status = outcome.status_code if isinstance(outcome, HttpError) else 200
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    gateway.auth.obtain_token = Mock(side_effect=obtain)
    return gateway.auth.obtain_token


def synthetic_token():
    return SimpleNamespace(access_token="SYNTHETIC_BEARER_TOKEN", expires_at=time.time() + 3600)


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "https://example.invalid" + PATHS["sleep"]),
        ("GET", "https://personal.connectapi.garmin.com" + PATHS["sleep"]),
        ("POST", BASE_URL + PATHS["sleep"]),
        ("DELETE", BASE_URL + PATHS["sleep"]),
        ("GET", BASE_URL + TOKEN_PATH),
        ("GET", BASE_URL + "/userprofile-service/userprofile/settings"),
        ("GET", BASE_URL + PATHS["sleep"] + "?date=2026-11-01"),
        ("GET", BASE_URL + PATHS["sleep"] + "#fragment"),
    ],
)
def test_scope_rejects_before_sending(method, url):
    with ScopedSession(BASE_URL) as session:
        with pytest.raises(GarminError, match="request_outside_allowed_scope"):
            session.request(method, url)
        assert session.request_count == 0


@pytest.mark.parametrize("method,path", [("GET", PATHS["sleep"]), ("POST", TOKEN_PATH)])
def test_allowed_response_is_bounded_cached_and_closed(monkeypatch, method, path):
    response = TrackingResponse(b'{"ok": true}')
    send = Mock(return_value=response)
    monkeypatch.setattr(requests.Session, "request", send)
    with ScopedSession(BASE_URL) as session:
        result = session.request(method, BASE_URL + path, allow_redirects=True, stream=False)
        assert result.json() == {"ok": True}
        assert response.was_closed
        assert send.call_args.kwargs["stream"] is True
        assert send.call_args.kwargs["allow_redirects"] is False
        assert session.request_count == 1


def test_redirect_closes_response_without_following(monkeypatch):
    response = TrackingResponse(status=302, headers={"Location": "https://example.invalid"})
    send = Mock(return_value=response)
    monkeypatch.setattr(requests.Session, "request", send)
    with ScopedSession(BASE_URL) as session:
        with pytest.raises(GarminError, match="redirect_blocked"):
            session.get(BASE_URL + PATHS["sleep"])
    assert response.was_closed
    assert send.call_count == 1


@pytest.mark.parametrize("declared", [False, True])
def test_oversized_response_is_rejected_and_closed(monkeypatch, declared):
    response = TrackingResponse(
        b"x" if declared else b"x" * (MAX_RESPONSE_BYTES + 1),
        headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)} if declared else {},
    )
    monkeypatch.setattr(requests.Session, "request", Mock(return_value=response))
    with ScopedSession(BASE_URL) as session:
        with pytest.raises(GarminError, match="response_too_large"):
            session.get(BASE_URL + PATHS["sleep"])
    assert response.was_closed


def test_response_at_size_limit_is_accepted(monkeypatch):
    response = TrackingResponse(b"x" * MAX_RESPONSE_BYTES)
    monkeypatch.setattr(requests.Session, "request", Mock(return_value=response))
    with ScopedSession(BASE_URL) as session:
        assert len(session.get(BASE_URL + PATHS["sleep"]).content) == MAX_RESPONSE_BYTES
    assert response.was_closed


def test_stream_failure_closes_response(monkeypatch):
    response = TrackingResponse()
    response.iter_content = Mock(side_effect=requests.Timeout(SYNTHETIC_SECRET))
    monkeypatch.setattr(requests.Session, "request", Mock(return_value=response))
    with ScopedSession(BASE_URL) as session:
        with pytest.raises(requests.Timeout):
            session.get(BASE_URL + PATHS["sleep"])
    assert response.was_closed


def test_timeout_retries_are_bounded(gateway):
    client = ready_client(gateway, [requests.Timeout(), requests.Timeout(), {"ok": True}])
    assert gateway.fetch(REQUEST).payload == {"ok": True}
    assert client.get.call_count == 3
    assert [call.args[0] for call in gateway.sleeper.call_args_list] == [0.2, 1.0, 0.2, 2.0, 0.2]


def test_timeout_exhaustion_is_safe(gateway):
    client = ready_client(gateway, [requests.Timeout(SYNTHETIC_SECRET)] * 3)
    with pytest.raises(GarminError, match="network_retry_exhausted") as caught:
        gateway.fetch(REQUEST)
    assert client.get.call_count == 3
    assert SYNTHETIC_SECRET not in "".join(traceback.format_exception(caught.value))


def test_401_after_two_transient_failures_still_retries_new_token(gateway, monkeypatch):
    client = ready_client(
        gateway,
        [http_error(503), http_error(503), http_error(401), {"ok": True}],
    )
    authenticate = Mock()
    monkeypatch.setattr(gateway, "authenticate", authenticate)
    assert gateway.fetch(REQUEST).payload == {"ok": True}
    authenticate.assert_called_once_with(force=True)
    assert client.get.call_count == 4


def test_second_401_does_not_refresh_again(gateway, monkeypatch):
    client = ready_client(gateway, [http_error(401), http_error(401)])
    authenticate = Mock()
    monkeypatch.setattr(gateway, "authenticate", authenticate)
    with pytest.raises(GarminError, match="authorization_failed"):
        gateway.fetch(REQUEST)
    authenticate.assert_called_once_with(force=True)
    assert client.get.call_count == 2


def test_refresh_does_not_reset_transient_budget(gateway, monkeypatch):
    client = ready_client(
        gateway,
        [http_error(503), http_error(503), http_error(401), http_error(503)],
    )
    monkeypatch.setattr(gateway, "authenticate", Mock())
    with pytest.raises(GarminError, match="retry_exhausted"):
        gateway.fetch(REQUEST)
    assert client.get.call_count == 4


def test_403_does_not_refresh(gateway, monkeypatch):
    client = ready_client(gateway, [http_error(403)])
    authenticate = Mock()
    monkeypatch.setattr(gateway, "authenticate", authenticate)
    with pytest.raises(GarminError, match="authorization_failed"):
        gateway.fetch(REQUEST)
    assert client.get.call_count == 1
    authenticate.assert_not_called()


def test_short_retry_after_is_observed(gateway):
    client = ready_client(gateway, [http_error(429), {"ok": True}], retry_after="3")
    assert gateway.fetch(REQUEST).payload == {"ok": True}
    assert client.get.call_count == 2
    assert [call.args[0] for call in gateway.sleeper.call_args_list] == [0.2, 3.0, 0.2]


def test_long_retry_after_defers_with_safe_metadata(gateway):
    client = ready_client(gateway, [http_error(429)], retry_after="120")
    with pytest.raises(GarminError, match="retry_after_deferred") as caught:
        gateway.fetch(REQUEST)
    assert client.get.call_count == 1
    assert caught.value.http_status == 429
    assert caught.value.retry_after_seconds == 120
    assert SYNTHETIC_SECRET not in "".join(traceback.format_exception(caught.value))
    gateway.sleeper.assert_called_once_with(0.2)


def test_http_date_retry_after_defers(gateway):
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    ready_client(gateway, [http_error(503)], retry_after=future)
    with pytest.raises(GarminError, match="retry_after_deferred") as caught:
        gateway.fetch(REQUEST)
    assert 118 <= caught.value.retry_after_seconds <= 120


@pytest.mark.parametrize("header", ["nonsense", "NaN", "inf"])
def test_invalid_retry_after_falls_back_to_finite_backoff(gateway, header):
    ready_client(gateway, [http_error(429), {}], retry_after=header)
    gateway.fetch(REQUEST)
    assert [call.args[0] for call in gateway.sleeper.call_args_list] == [0.2, 1.0, 0.2]


def test_last_429_retains_retry_timing(gateway):
    ready_client(gateway, [http_error(429)] * 3, retry_after="4")
    with pytest.raises(GarminError, match="retry_exhausted") as caught:
        gateway.fetch(REQUEST)
    assert caught.value.retry_after_seconds == 4


def test_authentication_retries_network_and_server_failures(gateway):
    obtain = fake_auth(gateway, [requests.Timeout(), http_error(503), synthetic_token()])
    gateway.authenticate()
    assert obtain.call_count == 3
    assert gateway.client.base_url == BASE_URL
    assert [call.args[0] for call in gateway.sleeper.call_args_list] == [1.0, 2.0]


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_rejection_is_not_retried_or_exposed(gateway, status, caplog):
    obtain = fake_auth(gateway, [http_error(status)])
    with pytest.raises(GarminError, match="authentication_rejected") as caught:
        gateway.authenticate()
    assert obtain.call_count == 1
    assert caught.value.http_status == status
    gateway.sleeper.assert_not_called()
    output = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert SYNTHETIC_SECRET not in output
    assert "SYNTHETIC_API_KEY" not in output


def test_authentication_429_uses_retry_after(gateway):
    obtain = fake_auth(gateway, [http_error(429), synthetic_token()], retry_after="2")
    gateway.authenticate()
    assert obtain.call_count == 2
    gateway.sleeper.assert_called_once_with(2.0)


def test_authentication_long_retry_after_defers(gateway):
    obtain = fake_auth(gateway, [http_error(429)], retry_after="30")
    with pytest.raises(GarminError, match="retry_after_deferred") as caught:
        gateway.authenticate()
    assert obtain.call_count == 1
    assert caught.value.retry_after_seconds == 30
    gateway.sleeper.assert_not_called()
    assert SYNTHETIC_SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "failure,code",
    [
        (requests.Timeout(SYNTHETIC_SECRET), "authentication_network_retry_exhausted"),
        (http_error(503), "authentication_retry_exhausted"),
    ],
)
def test_authentication_transient_failures_have_three_attempts(gateway, failure, code):
    obtain = fake_auth(gateway, [failure] * 3)
    with pytest.raises(GarminError, match=code) as caught:
        gateway.authenticate()
    assert obtain.call_count == 3
    assert SYNTHETIC_SECRET not in "".join(traceback.format_exception(caught.value))
