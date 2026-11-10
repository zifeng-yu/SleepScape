import os

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sleepscape.app import create_app
from sleepscape.config import Settings


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch):
    """Keep a developer's real account paths out of the test process."""
    for name in tuple(os.environ):
        if name.upper().startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)


def test_service_starts_without_garmin_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    with TestClient(
        create_app(settings), base_url="http://localhost", client=("127.0.0.1", 50000)
    ) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["service"] == "SleepScape"
        assert "garmin_key_file" not in response.json()
        assert client.get("/openapi.json").status_code == 404
    assert not settings.garmin_key_file.exists()
    assert not settings.garmin_token_cache.exists()


def test_dotenv_loads_region_and_private_file_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GARMIN_PERSONAL_API_BASE_URL", raising=False)
    monkeypatch.delenv("SLEEPSCAPE_GARMIN_KEY_FILE", raising=False)
    (tmp_path / ".env").write_text(
        "GARMIN_PERSONAL_API_BASE_URL=https://personal.connectapi.garmin.cn\n"
        "SLEEPSCAPE_GARMIN_KEY_FILE=.secrets/test-key\n",
        encoding="utf-8",
    )
    settings = Settings()
    assert settings.garmin_base_url == "https://personal.connectapi.garmin.cn"
    assert str(settings.garmin_key_file) == ".secrets/test-key"


def test_unknown_timezone_is_rejected():
    with pytest.raises(ValidationError, match="valid IANA timezone"):
        Settings(_env_file=None, timezone="Invalid/Timezone")
