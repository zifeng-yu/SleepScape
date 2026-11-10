"""HTTP boundaries use synthetic/private temporary state, never the user's account."""

import base64
import json

import pytest
import requests
from fastapi.testclient import TestClient

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.web.security import save_password


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Dashboard requests must never contact Garmin")

    monkeypatch.setattr(requests.Session, "send", forbidden)
    return Settings(_env_file=None, data_dir=tmp_path / "data")


def client(settings, *, peer="127.0.0.1", base_url="http://localhost"):
    return TestClient(create_app(settings), base_url=base_url, client=(peer, 12345))


def basic(username, password):
    return {
        "Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    }


def test_local_empty_dashboard_has_safe_state_and_never_creates_a_database(settings):
    with client(settings) as browser:
        response = browser.get("/api/v1/status")
        assert response.status_code == 200
        assert response.json()["database"]["present"] is False
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert browser.get("/api/v1/nights/2026-01-01").status_code == 404
        assert browser.get("/").status_code == 200
        assert browser.get("/static/app.js").status_code == 200
        assert browser.get("/openapi.json").status_code == 404
        assert browser.get("/docs").status_code == 404
        assert browser.get("/.env").status_code == 404
        assert browser.get("/data/sleepscape.sqlite3").status_code == 404
        assert browser.get("/.secrets/garmin-personal-api-key").status_code == 404
    assert not settings.data_dir.exists()


def test_nonlocal_peer_and_proxy_headers_cannot_bypass_local_only_access(settings):
    with client(settings, peer="192.0.2.4") as browser:
        assert browser.get("/").status_code == 403
        assert browser.get("/api/v1/status").status_code == 403
        assert browser.get("/health").status_code == 200
    with client(settings) as browser:
        assert browser.get("/", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        assert browser.get("/", headers={"Host": "evil.invalid"}).status_code == 400
        assert browser.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert (
            browser.get(
                "/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
            ).status_code
            == 200
        )
        assert browser.post("/api/v1/status").status_code == 405


def test_network_binding_requires_password_configuration(settings):
    settings.host = "0.0.0.0"
    with pytest.raises(ValueError, match="network_binding_requires_web_auth_file"):
        create_app(settings)


def test_optional_api_schema_is_local_and_does_not_load_external_documentation(settings):
    settings.debug_api_docs = True
    with client(settings) as browser:
        response = browser.get("/openapi.json")
        assert response.status_code == 200
        assert "/api/v1/nights/{day}" in response.json()["paths"]
        assert browser.get("/docs").status_code == 404
    with client(settings, peer="192.0.2.4") as browser:
        assert browser.get("/openapi.json").status_code == 403


def test_password_protects_pages_assets_and_data_and_rotation_invalidates_cache(settings, tmp_path):
    password = "synthetic-long-password"
    path = tmp_path / ".private" / "dashboard.json"
    save_password(path, "sleepscape", password)
    stored = json.loads(path.read_text())
    assert password not in path.read_text()
    assert stored["algorithm"] == "pbkdf2-sha256"
    assert path.stat().st_mode & 0o777 == 0o600
    settings.web_auth_file = path
    with client(settings, peer="192.0.2.4", base_url="https://localhost") as browser:
        for url in ("/", "/static/app.js", "/api/v1/status"):
            response = browser.get(url)
            assert response.status_code == 401
            assert response.headers["www-authenticate"].startswith("Basic")
        assert browser.get("/", headers=basic("wrong", password)).status_code == 401
        assert browser.get("/", headers={"Authorization": "Basic invalid!"}).status_code == 401
        assert browser.get("/", headers=basic("sleepscape", password)).status_code == 200
        assert (
            browser.get("/api/v1/status", headers=basic("sleepscape", password)).status_code == 200
        )
        save_password(path, "sleepscape", "another-synthetic-password")
        assert browser.get("/", headers=basic("sleepscape", password)).status_code == 401
        assert (
            browser.get("/", headers=basic("sleepscape", "another-synthetic-password")).status_code
            == 200
        )


def test_password_is_not_accepted_over_remote_plain_http_or_spoofed_proto(settings, tmp_path):
    path = tmp_path / ".private" / "dashboard.json"
    save_password(path, "sleepscape", "synthetic-long-password")
    settings.web_auth_file = path
    credentials = basic("sleepscape", "synthetic-long-password")
    with client(settings, peer="192.0.2.4") as browser:
        assert browser.get("/", headers=credentials).json() == {"error": "https_required"}
        assert (
            browser.get("/", headers={**credentials, "X-Forwarded-Proto": "https"}).status_code
            == 403
        )
        assert "www-authenticate" not in browser.get("/").headers
    with client(settings) as browser:
        assert browser.get("/", headers=credentials).status_code == 200
        assert (
            browser.get("/", headers={**credentials, "X-Forwarded-Proto": "http"}).status_code
            == 403
        )


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/nights?start=2026-01-01&end=2026-05-01",
        "/api/v1/nights?start=2026-02-01&end=2026-01-01",
        "/api/v1/nights?start=2026-01-01&end=2026-01-31&cursor=2025-12-01",
        "/api/v1/nights?limit=1000000",
        "/api/v1/nights/0001-01-01",
        "/api/v1/nights/not-a-date",
        "/api/v1/nights/2026-01-01/timeline?max_points=2001",
        "/api/v1/nights/2026-01-01/timeline?from=2026-01-01T00:00:00Z",
        "/api/v1/nights/2026-01-01/timeline?from=2026-01-01T00:00:00&to=2026-01-02T00:00:00",
        "/api/v1/nights/2026-01-01/timeline?from=2026-01-01T00:00:00Z&to=2026-01-04T00:00:00Z",
    ],
)
def test_date_time_and_size_limits_are_rejected_before_data_query(settings, url):
    with client(settings) as browser:
        response = browser.get(url)
        assert response.status_code == 422
        assert set(response.json()) == {"error"}


def test_internal_errors_never_expose_payloads_or_paths(settings, monkeypatch, caplog):
    from sleepscape.services.views import DashboardService

    def failed(self):
        raise RuntimeError("SYNTHETIC_PRIVATE_PAYLOAD_DO_NOT_EXPOSE")

    monkeypatch.setattr(DashboardService, "status", failed)
    with client(settings) as browser:
        response = browser.get("/api/v1/status")
        assert response.status_code == 503
        assert response.json() == {"error": "local_data_unavailable"}
    assert "SYNTHETIC_PRIVATE_PAYLOAD_DO_NOT_EXPOSE" not in caplog.text


def test_world_readable_password_file_is_rejected(settings, tmp_path):
    path = tmp_path / ".private" / "dashboard.json"
    save_password(path, "sleepscape", "synthetic-long-password")
    path.chmod(0o644)
    settings.web_auth_file = path
    with pytest.raises(ValueError, match="web_auth_configuration_invalid"):
        create_app(settings)
