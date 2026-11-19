"""Maintenance GET exposes bounded metadata, never executes or repairs maintenance."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import OpenerDirector

import pytest
import requests
from fastapi.testclient import TestClient

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.services import maintenance
from sleepscape.services.views import DashboardService
from sleepscape.storage import backup_manifest
from sleepscape.storage.repository import Repository

NOW = datetime(2026, 11, 17, 4, tzinfo=UTC)
PRIVATE = "SYNTHETIC_PRIVATE_BACKUP_DESTINATION"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(key)

    def forbidden(*args, **kwargs):
        pytest.fail("Maintenance GET must not execute maintenance or external requests")

    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr(OpenerDirector, "open", forbidden)
    monkeypatch.setattr(maintenance, "run_maintenance", forbidden)
    return Settings(
        _env_file=None,
        data_dir=tmp_path.resolve() / "data",
        llm_config_file=Path(__file__).resolve().parents[1] / "configs/models.toml",
    )


def client(settings, peer="127.0.0.1"):
    return TestClient(create_app(settings), base_url="http://localhost", client=(peer, 5000))


def test_maintenance_status_without_runtime_is_readonly_and_schedule_not_installed(settings):
    with client(settings) as browser:
        response = browser.get("/api/v1/status")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        data = response.json()["maintenance"]
        assert data["state"] == "not_run"
        assert data["latest"] is None
        assert data["schedule"]["time"] == "04:00"
        assert data["schedule"]["timezone"] == "Asia/Shanghai"
        assert data["schedule"]["installation_state"] == "unknown"
        assert data["offsite"]["status"] == "offsite_not_configured"
        assert browser.get("/static/maintenance.js").status_code == 200
    assert not settings.data_dir.exists()


def test_corrupt_maintenance_state_does_not_break_daily_model_or_database_status(settings):
    with Repository(settings.data_dir):
        pass
    path = settings.data_dir / backup_manifest.STATE_FILE
    path.write_text('{"private":"' + PRIVATE + '","broken":')
    path.chmod(0o600)
    original = path.read_bytes(), path.stat().st_mtime_ns
    with client(settings) as browser:
        response = browser.get("/api/v1/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["maintenance"]["state"] == "unavailable"
    assert payload["database"]["present"] is True
    assert payload["daily"]["state"] == "not_run"
    assert payload["model"]["configured"] is False
    assert "environment" in payload
    assert PRIVATE not in json.dumps(payload)
    assert str(path) not in json.dumps(payload)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original


def test_maintenance_public_metadata_never_exports_bundle_ids_or_private_files(settings):
    state = backup_manifest.initial_state()
    state["bundles"] = [
        {
            "id": "a" * 32,
            "created_at": "2026-11-16T20:00:00Z",
            "size_bytes": 123456,
            "manifest_sha256": "b" * 64,
            "status": "verified",
        }
    ]
    state["runs"] = [
        {
            "day": "2026-11-17",
            "status": "completed",
            "phase": "finished",
            "started_at": "2026-11-16T20:00:00Z",
            "finished_at": "2026-11-16T20:02:00Z",
            "error_code": None,
            "backup_id": "a" * 32,
        }
    ]
    backup_manifest.write_state(settings.data_dir, state)
    path = settings.data_dir / backup_manifest.STATE_FILE
    original = path.read_bytes(), path.stat().st_mtime_ns
    service = DashboardService(settings)
    service._now = lambda: NOW
    payload = service.status()["maintenance"]
    assert payload["state"] == "available"
    assert payload["backups"]["verified_count"] == 1
    assert payload["backups"]["total_bytes"] == 123456
    assert payload["latest"]["status"] == "completed"
    encoded = json.dumps(payload)
    for private in ("a" * 32, "b" * 64, "backup_id", "manifest_sha256", str(settings.data_dir)):
        assert private not in encoded
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    assert not (settings.data_dir / "sleepscape.sqlite3").exists()
    assert not (settings.data_dir / "backups").exists()


def test_maintenance_status_reuses_access_control_and_has_no_mutating_http_endpoint(settings):
    with client(settings, "192.0.2.20") as browser:
        assert browser.get("/api/v1/status").status_code == 403
        assert browser.get("/static/maintenance.js").status_code == 403
    with client(settings) as browser:
        assert browser.post("/api/v1/status").status_code == 405
        assert browser.post("/api/v1/maintenance-run").status_code == 405
    assert not settings.data_dir.exists()


def test_offsite_metadata_preserves_budget_warning_and_resets_after_target_change(
    settings, monkeypatch
):
    settings.maintenance_export_mode = "path"
    settings.maintenance_export_path = settings.data_dir.parent / "private-target"
    state = backup_manifest.initial_state()
    state["export_fingerprint"] = maintenance._export_fingerprint(settings)
    state["offsite"].update(
        status="verified",
        mode="path",
        last_success_at=NOW.isoformat(),
        retained_count=1,
        retained_bytes=1048576,
        over_budget=True,
        location_state="location_unverified",
    )
    backup_manifest.write_state(settings.data_dir, state)
    original = (settings.data_dir / backup_manifest.STATE_FILE).read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only status must not inspect or hash backup contents")

    monkeypatch.setattr(maintenance, "validate_bundle", forbidden)
    monkeypatch.setattr(maintenance, "_legacy_inventory", forbidden)
    status = maintenance.maintenance_status(settings, now=NOW)
    assert status["offsite"]["status"] == "verified"
    assert status["offsite"]["over_budget"] is True
    assert status["offsite"]["retained_count"] == 1
    assert status["offsite"]["retained_bytes"] == 1048576
    assert status["offsite"]["location_verified"] is False
    assert "export_fingerprint" not in json.dumps(status)
    assert "private-target" not in json.dumps(status)

    settings.maintenance_export_path = settings.data_dir.parent / "new-private-target"
    status = maintenance.maintenance_status(settings, now=NOW)
    assert status["offsite"]["status"] == "not_checked"
    assert status["offsite"]["last_success_at"] is None
    assert status["offsite"]["retained_count"] is None
    assert (settings.data_dir / backup_manifest.STATE_FILE).read_bytes() == original
    assert not settings.maintenance_export_path.exists()
