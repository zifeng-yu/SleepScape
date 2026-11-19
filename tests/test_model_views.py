"""Public readiness/model reads stay offline, private and non-mutating."""

import json
from datetime import date
from pathlib import Path
from urllib.request import OpenerDirector

import pytest
from fastapi.testclient import TestClient

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.services import model_health
from sleepscape.services.analysis_views import analysis_view
from sleepscape.services.views import DashboardService


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("read-only analysis/status must not call the provider")

    monkeypatch.setattr(OpenerDirector, "open", forbidden)
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        llm_config_file=Path(__file__).resolve().parents[1] / "configs" / "models.toml",
    )


def test_read_only_empty_endpoints_do_not_create_state(settings):
    with TestClient(
        create_app(settings), base_url="http://localhost", client=("127.0.0.1", 5000)
    ) as client:
        for url in ("/api/v1/status", "/api/v1/nights/2026-11-16/analysis"):
            response = client.get(url)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
        view = response.json()
        assert view["data_state"] in {"waiting_data", "expired_unanalyzed"}
        assert view["report"] is None
        assert view["implementation_stage"] == "reports"
        assert view["execution_state"] != "blocked_implementation"
        assert view["model"]["configuration_state"] == "not_configured"
        assert len(view["missing"]) == 6
        assert {item["source"] for item in view["missing"]} == {"garmin"}
        assert client.post("/api/v1/nights/2026-11-16/analysis").status_code == 405
        assert client.get("/api/v1/nights/1999-01-01/analysis").status_code == 422
        assert client.get("/api/v1/nights/not-a-date/analysis").status_code == 422
    assert not settings.data_dir.exists()


def test_analysis_route_uses_existing_access_gate(settings):
    with TestClient(
        create_app(settings), base_url="http://localhost", client=("192.0.2.10", 5000)
    ) as client:
        assert client.get("/api/v1/nights/2026-11-16/analysis").status_code == 403


def test_history_reports_no_saved_check_without_database(settings):
    service = DashboardService(settings)
    view = service.nights(date(2026, 11, 15), date(2026, 11, 16))
    assert {row["analysis_summary"]["state"] for row in view["items"]} == {"not_evaluated"}
    status = service.status()
    assert status["model"]["configured"] is False
    assert (
        next(item for item in status["capabilities"] if item["key"] == "environment")["note"]
        == "not_initialized"
    )
    assert not settings.data_dir.exists()


def test_configured_reads_never_open_key_or_trigger_check(settings, tmp_path, monkeypatch):
    config = Path(__file__).resolve().parents[1] / "configs" / "models.toml"
    private = tmp_path / ".private"
    private.mkdir(mode=0o700)
    key = private / "key"
    key.write_text("SYNTHETIC_SECRET_MUST_NOT_APPEAR")
    key.chmod(0o600)
    settings.llm_config_file = config
    settings.llm_enabled = True
    settings.llm_base_url = "https://private-provider.example/v1"
    settings.llm_key_file = key
    original = Path.open

    def safe_open(path, *args, **kwargs):
        assert path != key, "GET must stat, not read, a credential"
        return original(path, *args, **kwargs)

    def no_live(*args, **kwargs):
        pytest.fail("GET cannot run model checks")

    monkeypatch.setattr(Path, "open", safe_open)
    monkeypatch.setattr(model_health, "run_model_check", no_live)
    view = analysis_view(settings, date(2026, 11, 16))
    assert view["implementation_stage"] == "reports"
    assert view["execution_state"] != "blocked_implementation"
    assert view["model"]["connection_state"] == "not_checked"
    assert view["data_state"] in {"waiting_data", "expired_unanalyzed"}
    encoded = json.dumps(view)
    for hidden in ("private-provider", "SYNTHETIC_SECRET", str(key), "endpoint", "fingerprint"):
        assert hidden not in encoded
    assert not settings.data_dir.exists()
