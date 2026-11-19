"""Display-only date bounds retain history, overnight observations and collection plans."""

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.request import OpenerDirector

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import sleepscape.app as app_module
from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.domain.dashboard_dates import NightBeforeDisplayStart
from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.services.analysis_views import analysis_view
from sleepscape.services.daily import prepare_daily
from sleepscape.services.views import DashboardService
from sleepscape.storage import environment
from sleepscape.storage.environment_index import migrate_index, rebuild_index
from sleepscape.storage.reader import DashboardReader
from sleepscape.storage.repository import Repository

START = date(2026, 11, 18)
ROOT = Path(__file__).resolve().parents[1]


def _client(settings):
    return TestClient(create_app(settings), base_url="http://localhost", client=("127.0.0.1", 5000))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        OpenerDirector, "open", lambda *_args, **_kwargs: pytest.fail("no network in view tests")
    )
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        dashboard_start_date=START,
        llm_config_file=ROOT / "configs" / "models.toml",
        demo=True,
    )


def _save(repository, run, resource, records, start, end=None, metrics=None):
    request = DataRequest(
        resource,
        f"synthetic:{start}:{end or start}",
        "/synthetic",
        start_date=str(start),
        end_date=str(end or start),
    )
    repository.save_snapshot(
        run,
        FetchResult(request, {"synthetic": records}, "2026-11-21T01:00:00Z", 200),
        NormalizedBatch(records, metrics=metrics or {}),
    )


def _seed_nights(settings, days):
    with Repository(settings.data_dir) as repository:
        run = repository.start_run({"end_date": str(max(days))})
        for day in days:
            _save(
                repository,
                run,
                "sleep",
                [
                    {
                        "kind": "sleep",
                        "key": str(day),
                        "date": str(day),
                        "timestamp": None,
                        "data": {
                            "sleep_seconds": 28000,
                            "start_at": f"{day - timedelta(days=1)}T15:00:00Z",
                            "end_at": f"{day - timedelta(days=1)}T23:00:00Z",
                        },
                    }
                ],
                day,
            )
        repository.finish_run(run, "completed", {})


def _facts_digest(settings):
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        return hashlib.sha256(
            json.dumps(
                [
                    connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in ("normalized_records", "raw_snapshots", "requests", "runs")
                ],
                default=lambda value: value.hex(),
            ).encode()
        ).hexdigest()


@pytest.mark.parametrize("value", [None, "", "2026-11-18", START, "2000-01-01", "2100-12-31"])
def test_optional_start_configuration_accepts_only_supported_calendar_dates(value):
    settings = Settings(_env_file=None, dashboard_start_date=value)
    expected = date.fromisoformat(value) if isinstance(value, str) and value else value or None
    assert settings.dashboard_start_date == expected


@pytest.mark.parametrize(
    "value",
    ["20261118", "2026-02-30", "2026-11-18T00:00:00Z", "1999-12-31", "2101-01-01", 0, True],
)
def test_invalid_start_configuration_is_rejected(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dashboard_start_date=value)


def test_default_none_retains_existing_behavior_and_history(settings):
    _seed_nights(settings, [START - timedelta(days=1)])
    unbounded = settings.model_copy(update={"dashboard_start_date": None})
    service = DashboardService(unbounded)
    assert service.status()["dashboard_start_date"] is None
    assert service.status()["earliest_night"] == "2026-11-17"
    assert service.night(START - timedelta(days=1))["date"] == "2026-11-17"


def test_status_filters_actual_minimum_and_maximum_without_hiding_storage_inventory(settings):
    _seed_nights(settings, [START - timedelta(days=100), START + timedelta(days=2)])
    before = _facts_digest(settings)
    service = DashboardService(settings)
    status = service.status()
    assert status["dashboard_start_date"] == "2026-11-18"
    assert status["earliest_night"] == status["latest_night"] == "2026-11-20"
    assert status["database"]["record_count"] == 2
    with DashboardReader(settings.data_dir) as reader:
        assert reader.status()["earliest_night"] == str(START - timedelta(days=100))
    assert _facts_digest(settings) == before


def test_future_start_without_visible_sleep_uses_empty_start_date_page(settings, monkeypatch):
    _seed_nights(settings, [START - timedelta(days=1)])

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            now = datetime(2026, 11, 17, 14, tzinfo=UTC)
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    browser = _client(settings)
    monkeypatch.setattr(app_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(DashboardService, "_now", lambda _self: FrozenDatetime.now(UTC))
    before = _facts_digest(settings)
    with browser as client:
        status = client.get("/api/v1/status").json()
        assert status["latest_night"] is None and status["earliest_night"] is None
        history = client.get("/api/v1/nights").json()
        assert [item["date"] for item in history["items"]] == ["2026-11-18"]
        assert history["items"][0]["sleep_seconds"]["value"] is None
        assert history["available_dates"] == [] and history["next_cursor"] is None
        assert client.get("/api/v1/nights/2026-11-18").status_code == 404
        assert client.get("/api/v1/nights/2026-11-18/analysis").json()["report"] is None
    assert _facts_digest(settings) == before


def test_hidden_night_urls_and_direct_services_are_rejected_without_exposing_history(settings):
    hidden = START - timedelta(days=1)
    _seed_nights(settings, [hidden, START])
    before = _facts_digest(settings)
    with _client(settings) as client:
        for suffix in ("", "/timeline", "/analysis"):
            response = client.get(f"/api/v1/nights/{hidden}{suffix}")
            assert response.status_code == 404
            assert response.json() == {"error": "night_before_dashboard_start"}
        assert client.get(f"/api/v1/nights/{START}").status_code == 200
    service = DashboardService(settings)
    for operation in (service.night, service.timeline, lambda d: analysis_view(settings, d)):
        with pytest.raises(NightBeforeDisplayStart):
            operation(hidden)
    assert _facts_digest(settings) == before


def test_history_clamps_pagination_and_empty_intersection(settings):
    _seed_nights(settings, [START - timedelta(days=1), START, START + timedelta(days=2)])
    service = DashboardService(settings)
    first = START - timedelta(days=3)
    end = START + timedelta(days=2)
    page = service.nights(first, end, limit=2)
    assert [item["date"] for item in page["items"]] == ["2026-11-20", "2026-11-19"]
    assert page["next_cursor"] == "2026-11-18"
    last = service.nights(first, end, cursor=START, limit=2)
    assert [item["date"] for item in last["items"]] == ["2026-11-18"]
    assert last["next_cursor"] is None
    assert last["available_dates"] == ["2026-11-20", "2026-11-18"]
    for page in (
        service.nights(first, START - timedelta(days=1)),
        service.nights(first, end, cursor=START - timedelta(days=1)),
    ):
        assert page["items"] == [] and page["available_dates"] == []
        assert page["next_cursor"] is None
    with _client(settings) as client:
        response = client.get("/api/v1/nights?start=2026-11-15&end=2026-11-17")
        assert response.status_code == 200 and response.json()["items"] == []


def test_old_load_baseline_and_daily_collection_plan_are_retained(settings):
    _seed_nights(settings, [START])
    first = START - timedelta(days=31)
    last = START - timedelta(days=1)
    with Repository(settings.data_dir) as repository:
        run = repository.start_run({"end_date": str(START)})
        _save(
            repository,
            run,
            "activities",
            [
                {
                    "kind": "activity",
                    "key": str(first + timedelta(days=offset)),
                    "date": str(first + timedelta(days=offset)),
                    "timestamp": None,
                    "data": {"training_load": 300 if offset == 30 else 100},
                }
                for offset in range(31)
            ],
            first,
            last,
            {"pagination_complete": True},
        )
        repository.finish_run(run, "completed", {})
    before = _facts_digest(settings)
    comparison = DashboardService(settings).night(START)["load_comparison"]
    assert comparison["target_load"] == 300 and comparison["baseline_mean"] == 100
    assert comparison["baseline_valid_days"] == 30 and comparison["change_percent"] == 200
    unbounded = settings.model_copy(update={"dashboard_start_date": None})
    assert prepare_daily(settings, now=datetime(2026, 11, 18, 1, tzinfo=UTC)) == prepare_daily(
        unbounded, now=datetime(2026, 11, 18, 1, tzinfo=UTC)
    )
    assert _facts_digest(settings) == before


def test_first_visible_night_includes_previous_evening_environment(settings):
    payload = json.loads((ROOT / "tests/fixtures/environment-batch.json").read_text())
    payload["time_anchors"][0]["utc_at"] = "2026-11-17T15:00:00Z"
    profile = MeasurementProfile.model_validate_json(
        (ROOT / "tests/fixtures/environment-profile.json").read_text()
    )
    environment.register_local(
        settings.data_dir, payload["device_id"], "synthetic-profile", profile
    )
    environment.ingest(
        settings.data_dir, payload["device_id"], EnvironmentBatch.model_validate(payload)
    )
    migrate_index(settings.data_dir)
    rebuild_index(settings.data_dir)
    with _client(settings) as client:
        night = client.get("/api/v1/nights/2026-11-18")
        assert night.status_code == 200 and night.json()["environment_only"] is True
        timeline = client.get("/api/v1/nights/2026-11-18/timeline").json()
        observed = [series for series in timeline["series"] if series["points"]]
        assert timeline["environment"]["record_count"] == 1 and len(observed) == 5
        assert observed[0]["points"][0]["window_start"] == "2026-11-17T15:00:00Z"
        independent = client.get(
            "/api/v1/environment/timeline",
            params={"from": "2026-11-17T14:00:00Z", "to": "2026-11-17T16:00:00Z"},
        )
        assert independent.status_code == 200 and len(independent.json()["series"]) == 5
