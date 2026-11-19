import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.services.backup import BackupError, check_restore, create_backup
from sleepscape.services.morning_feeling import prepare
from sleepscape.storage import morning_feeling
from sleepscape.storage.usage import CapacityError
from sleepscape.web.security import save_password


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None, data_dir=tmp_path / "data", dashboard_start_date=date(2026, 1, 1)
    )


URL = "/api/v1/nights/2026-01-07/morning-feeling"
ORIGIN = {"Origin": "http://localhost"}


def test_empty_install_update_and_recover(settings, monkeypatch):
    with TestClient(create_app(settings), base_url=ORIGIN["Origin"], client=("127.0.0.1", 80)) as c:
        assert c.get(URL).status_code == 503
        assert not settings.data_dir.exists()
        assert prepare(settings)["backup_verified"]
        assert prepare(settings)["status"] == "already_initialized"
        assert c.get(URL.removesuffix("/morning-feeling")).status_code == 404
        assert c.get(URL).json() is None

        def put(value, url=URL):
            return c.put(url, json={"feeling": value}, headers=ORIGIN)

        first = put("good").json()
        assert put("good").json() == first
        edited = put("poor").json()
        assert edited["filled_at"] > first["filled_at"]
        previous = URL.replace("01-07", "01-06")
        assert put("fair", previous).json()["night_date"] == "2026-01-06"
        assert c.get(URL).json() == edited
        assert put("fair", URL.replace("2026-01-07", "2025-12-31")).status_code == 404
        assert put("unknown").status_code == 422
        assert (
            c.put(URL, json={"feeling": "good", "filled_at": "x"}, headers=ORIGIN).status_code
            == 422
        )

        def reject(*args):
            raise CapacityError("storage_capacity_critical")

        monkeypatch.setattr(morning_feeling, "require_capacity", reject)
        assert put("good").status_code == 503
        assert c.get(URL).json() == edited
    backup = create_backup(settings)
    assert check_restore(settings, Path(backup["backup_path"]))["counts"]["morning_feeling"] == 2
    with sqlite3.connect(settings.data_dir / backup["backup_path"]) as db:
        assert db.execute(
            "select feeling,filled_at from morning_feeling where night_date=?", ("2026-01-07",)
        ).fetchone() == ("poor", edited["filled_at"])
        db.execute("update morning_feeling set filled_at='invalid'")
    with pytest.raises(BackupError, match="backup_morning_feeling_invalid"):
        check_restore(settings, Path(backup["backup_path"]))


def test_authenticated_same_origin_write_only(settings, tmp_path):
    settings.web_auth_file = tmp_path / "auth"
    save_password(settings.web_auth_file, "owner", "synthetic-password")
    prepare(settings)
    with TestClient(
        create_app(settings), base_url="https://localhost", client=("192.0.2.9", 443)
    ) as c:

        def write(origin):
            return c.put(URL, headers={"Origin": origin}, json={"feeling": "fair"})

        assert write("https://localhost").status_code == 401
        assert c.get("/sleep", auth=("owner", "synthetic-password")).status_code == 200
        assert write("https://other.invalid").status_code == 403
        assert c.put(URL, json={"feeling": "good"}).status_code == 403
        assert write("https://localhost").status_code == 200
        assert c.put(URL + "/extra", headers={"Origin": "https://localhost"}).status_code == 405
