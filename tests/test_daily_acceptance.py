"""Independent P5 acceptance with synthetic sources and no network or account access."""

import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event
from urllib.request import OpenerDirector

import pytest
import requests

from sleepscape.adapters.catalog import daily_request
from sleepscape.adapters.garmin import GarminError
from sleepscape.config import Settings
from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.domain.models import FetchResult
from sleepscape.services import daily
from sleepscape.services.collection import CollectionError, collect
from sleepscape.services.views import DashboardService
from sleepscape.storage import daily_runtime, environment
from sleepscape.storage.repository import Repository

DAY = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 1, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper().startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Daily acceptance must not contact an account, model, or network")

    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr(OpenerDirector, "open", forbidden)
    monkeypatch.setattr("sleepscape.services.collection.GarminGateway", forbidden)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        garmin_lookback_days=7,
        garmin_key_file=tmp_path / "unused-key",
        garmin_token_cache=tmp_path / "unused-token",
    )


class Source:
    """Supply successful empty resources, optionally one late sleep observation."""

    def __init__(self, *, at=NOW, sleep_day=None, entered=None, release=None):
        self.calls = []
        self.at = at
        self.sleep_day = sleep_day
        self.entered = entered
        self.release = release

    def fetch(self, request):
        self.calls.append(request)
        if self.entered is not None and len(self.calls) == 1:
            self.entered.set()
            assert self.release.wait(10), "Synthetic network pause was not released"
        payload = [] if request.resource == "activities_page" else None
        if request.resource == "sleep" and request.start_date == self.sleep_day:
            begin = datetime.combine(DAY - timedelta(days=1), datetime.min.time(), UTC)
            begin += timedelta(hours=15)
            payload = {
                "calendarDate": self.sleep_day,
                "sleepTimeSeconds": 28800,
                "sleepStartTimestampGMT": int(begin.timestamp() * 1000),
                "sleepEndTimestampGMT": int((begin + timedelta(hours=8)).timestamp() * 1000),
                "avgHeartRate": 55,
                "deepSleepSeconds": 3600,
                "lightSleepSeconds": 21600,
                "remSleepSeconds": 3600,
                "awakeSleepSeconds": 0,
            }
        return FetchResult(request, payload, self.at.isoformat(), 200)


def test_late_sleep_backfill_updates_existing_dashboard_without_same_day_refetch(settings):
    source = Source()
    first = daily.run_daily(settings, now=NOW, gateway=source)
    assert first["status"] == "completed"
    assert first["analysis"] == "disabled"
    assert source.calls
    view = DashboardService(settings)
    view._now = lambda: NOW
    assert view.nights(DAY, DAY)["items"][0]["sleep_seconds"]["value"] is None

    repeated = Source()
    daily.run_daily(settings, now=NOW + timedelta(minutes=30), gateway=repeated)
    assert repeated.calls == []

    next_day = NOW + timedelta(days=1)
    late = Source(at=next_day, sleep_day=DAY.isoformat())
    result = daily.run_daily(settings, now=next_day, gateway=late)
    assert result["status"] == "completed"
    view._now = lambda: next_day
    night = view.night(DAY)
    assert night["cards"]["sleep_seconds"]["value"] == 28800
    assert night["cards"]["sleep_seconds"]["state"] == "available"
    assert night["sleep"]["start_at"] == "2026-11-16T15:00:00Z"
    assert night["sleep"]["end_at"] == "2026-11-16T23:00:00Z"
    assert view.timeline(DAY) is not None
    with Repository(settings.data_dir) as repository:
        observations = repository.list_records(kind="sleep", resource="sleep")
        assert len(observations) == 1
        assert observations[0]["date"] == DAY.isoformat()


def test_environment_ingestion_commits_while_daily_network_fetch_is_blocked(settings):
    profile = MeasurementProfile.model_validate_json(
        (FIXTURES / "environment-profile.json").read_bytes()
    )
    payload = EnvironmentBatch.model_validate_json(
        (FIXTURES / "environment-batch.json").read_bytes()
    )
    environment.register(
        settings.data_dir,
        "synthetic-device",
        "synthetic-profile",
        profile,
        "synthetic_" + "x" * 43,
    )
    entered, release = Event(), Event()
    source = Source(entered=entered, release=release)
    with ThreadPoolExecutor(max_workers=2) as executor:
        collection = executor.submit(daily.run_daily, settings, now=NOW, gateway=source)
        try:
            assert entered.wait(5), "Daily task never entered its synthetic fetch"
            ingest = executor.submit(
                environment.ingest, settings.data_dir, payload.device_id, payload
            )
            ack = ingest.result(timeout=5)
            assert ack["records"] == [{"record_type": "minute", "seq": 0, "status": "accepted"}]
            assert not collection.done()
        finally:
            release.set()
        assert collection.result(timeout=10)["status"] == "completed"
    counts = environment.status(settings.data_dir)["counts"]
    assert counts["environment_records"] == 1


def test_completed_database_recovers_unfinished_ledger_without_refetch(settings, monkeypatch):
    running_snapshots = []
    write_state = daily_runtime.write_state

    def remember(data_dir, state):
        if state["days"]:
            current = state["days"][-1]
            if current["status"] == "running" and current["run_id"]:
                running_snapshots.append(copy.deepcopy(state))
        return write_state(data_dir, state)

    monkeypatch.setattr(daily, "write_state", remember)
    finished = daily.run_daily(settings, now=NOW, gateway=Source())
    assert finished["status"] == "completed"
    assert running_snapshots, "Expected durable in-flight identity before fetching"
    # Equivalent to process death after the SQLite completion commit and before
    # the final atomic JSON replacement: both facts are individually valid.
    write_state(settings.data_dir, running_snapshots[-1])
    with Repository(settings.data_dir) as repository:
        before = repository.counts()
        assert repository.latest_run()["status"] == "completed"

    source = Source()
    recovered = daily.run_daily(settings, now=NOW + timedelta(minutes=2), gateway=source)
    assert recovered["status"] == "completed"
    assert source.calls == []
    with Repository(settings.data_dir) as repository:
        assert repository.counts() == before
    assert daily_runtime.read_state(settings.data_dir)["days"][-1]["status"] == "completed"


@pytest.mark.parametrize("first_mode", ["manual", "daily"])
def test_retry_after_survives_switch_between_daily_and_manual(settings, first_mode):
    now = datetime.now(UTC)

    class Limited(Source):
        def fetch(self, request):
            self.calls.append(request)
            raise GarminError("retry_after_deferred", 429, retry_after_seconds=3600)

    limited = Limited()
    manual_plan = [daily_request("sleep", DAY - timedelta(days=1))]
    if first_mode == "manual":
        collect(settings, DAY - timedelta(days=1), plan=manual_plan, gateway=limited)
    else:
        daily.run_daily(settings, now=now, gateway=limited)
    assert len(limited.calls) == 1

    blocked = Source()
    if first_mode == "manual":
        result = daily.run_daily(settings, now=now + timedelta(seconds=1), gateway=blocked)
        assert result["status"] == "deferred"
    else:
        with pytest.raises(CollectionError, match="retry_after_wait_still_active"):
            collect(settings, DAY - timedelta(days=1), plan=manual_plan, gateway=blocked)
    assert blocked.calls == []
    with Repository(settings.data_dir) as repository:
        assert repository.counts()["runs"] == 1


def test_readonly_status_rejects_fifo_lock_without_blocking(settings):
    settings.data_dir.mkdir(mode=0o700)
    os.mkfifo(settings.data_dir / daily_runtime.LOCK_FILE, mode=0o600)
    # A subprocess timeout makes a blocking FIFO-open regression fail cleanly,
    # without leaving a blocked thread inside the test runner.
    probe = """
import json
import sys
from pathlib import Path
from sleepscape.config import Settings
from sleepscape.services.daily import daily_status
settings = Settings(_env_file=None, data_dir=Path(sys.argv[1]))
print(json.dumps(daily_status(settings)))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(settings.data_dir)],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    status = json.loads(result.stdout)
    assert status["state"] == "unavailable"
    assert status["error_code"] == "daily_state_unavailable"
    assert not (settings.data_dir / daily_runtime.STATE_FILE).exists()
    assert not (settings.data_dir / "sleepscape.sqlite3").exists()
