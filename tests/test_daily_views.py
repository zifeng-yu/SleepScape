"""P5 public status is bounded metadata; GET cannot collect, mutate, or call a model."""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import OpenerDirector

import pytest
import requests
from fastapi.testclient import TestClient

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.services import daily
from sleepscape.services.views import DashboardService
from sleepscape.storage.repository import Repository

NOW = datetime(2026, 11, 17, 2, tzinfo=UTC)
PRIVATE = "SYNTHETIC_PRIVATE_ACCOUNT_VALUE"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("GET daily status must not invoke a network or collection operation")

    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr(OpenerDirector, "open", forbidden)
    monkeypatch.setattr(daily, "run_daily", forbidden)
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        garmin_lookback_days=7,
        llm_config_file=Path(__file__).resolve().parents[1] / "configs" / "models.toml",
    )


def client(settings, peer="127.0.0.1"):
    return TestClient(create_app(settings), base_url="http://localhost", client=(peer, 5000))


def stored_sleep(settings):
    with Repository(settings.data_dir) as repository:
        run = repository.start_run({"end_date": "2026-11-17", "detail_start": "2026-11-11"})
        request = DataRequest(
            "sleep", "2026-11-17", "/synthetic", start_date="2026-11-17", end_date="2026-11-17"
        )
        record = {
            "kind": "sleep",
            "key": "2026-11-17",
            "date": "2026-11-17",
            "timestamp": None,
            "data": {
                "start_at": "2026-11-16T15:00:00Z",
                "end_at": "2026-11-16T23:00:00Z",
                "sleep_seconds": 28000,
                "average_heart_rate": 55,
                "owner": PRIVATE,
            },
        }
        repository.save_snapshot(
            run,
            FetchResult(request, {"private": PRIVATE}, NOW.isoformat(), 200),
            NormalizedBatch([record]),
        )
        repository.finish_run(run, "completed", {})


def test_empty_status_get_does_not_create_state_or_collect(settings):
    with client(settings) as browser:
        response = browser.get("/api/v1/status")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["daily"]["state"] == "not_run"
        assert payload["daily"]["latest"] is None
        assert payload["daily"]["lookback_days"] == 7
        assert payload["daily"]["schedule"]["installation_state"] == "unknown"
        assert payload["daily"]["schedule"]["timezone"] == "Asia/Shanghai"
        assert payload["daily"]["schedule"]["time"] == "09:00"
        assert payload["daily"]["analysis"] == "automatic_disabled"
        assert browser.get("/static/daily.js").status_code == 200
    assert not settings.data_dir.exists()


def test_corrupt_daily_ledger_isolated_from_stored_garmin_and_model_cards(settings):
    stored_sleep(settings)
    path = settings.data_dir / "daily-runtime.json"
    path.write_text('{"private": "' + PRIVATE + '", "broken":')
    path.chmod(0o600)
    original = path.read_bytes(), path.stat().st_mtime_ns
    with client(settings) as browser:
        response = browser.get("/api/v1/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["daily"]["state"] == "unavailable"
    assert payload["database"]["present"] is True
    assert payload["latest_night"] == "2026-11-17"
    assert payload["model"]["configured"] is False
    assert "environment" in payload
    assert PRIVATE not in json.dumps(payload)
    assert str(path) not in json.dumps(payload)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original


def test_daily_metadata_never_exports_sleep_values_or_private_fields(settings):
    stored_sleep(settings)
    service = DashboardService(settings)
    service._now = lambda: NOW
    status = service.status()["daily"]
    encoded = json.dumps(status)
    for private in (
        PRIVATE,
        "average_heart_rate",
        "sleep_seconds",
        "run_id",
        str(settings.data_dir),
    ):
        assert private not in encoded
    assert status["analysis"] == "automatic_disabled"
    assert len(status["observations"]) == 7
    assert status["observations"][-1] == {"date": "2026-11-17", "sleep": "available"}
    assert {item["sleep"] for item in status["observations"][:-1]} == {"not_collected"}
    assert all(set(item) == {"date", "sleep"} for item in status["observations"])
    assert not (settings.data_dir / "daily-runtime.json").exists()
    assert not (settings.data_dir / "model-runtime.json").exists()


def test_daily_status_reuses_access_gate_and_no_start_endpoint(settings):
    with client(settings, "192.0.2.20") as browser:
        assert browser.get("/api/v1/status").status_code == 403
        assert browser.get("/static/daily.js").status_code == 403
    with client(settings) as browser:
        assert browser.post("/api/v1/status").status_code == 405
        assert browser.post("/api/v1/daily-run").status_code == 405
    assert not settings.data_dir.exists()
