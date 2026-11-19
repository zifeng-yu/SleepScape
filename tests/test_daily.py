"""Daily collection is synthetic, bounded and explicit; never use real keys or endpoints."""

import json
import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import requests

from sleepscape.adapters.garmin import GarminError
from sleepscape.config import Settings
from sleepscape.domain.calendar import dates
from sleepscape.domain.daily import schedule
from sleepscape.domain.models import FetchResult
from sleepscape.services.collection import CollectionError, collect, collection_lock
from sleepscape.services.daily import daily_status, prepare_daily, run_daily
from sleepscape.storage.daily_runtime import (
    COOLDOWN_FILE,
    MAX_DAYS,
    STATE_FILE,
    defer_until,
    read_state,
    write_state,
)
from sleepscape.storage.repository import Repository

NOW = datetime(2026, 3, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        pytest.fail("No network, model calls or credentials in synthetic daily tests")

    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr("sleepscape.services.collection.GarminGateway", forbidden)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Settings(
        _env_file=None,
        data_dir=tmp_path.resolve() / "data",
        garmin_key_file=tmp_path / "unused-key",
        garmin_token_cache=tmp_path / "unused-token",
    )


class Gateway:
    def __init__(self, error=None, interrupt=False):
        self.calls = []
        self.error = error
        self.interrupt = interrupt
        self.session = SimpleNamespace(request_count=0)

    def fetch(self, request):
        self.calls.append(request)
        self.session.request_count += 1
        if self.interrupt:
            raise KeyboardInterrupt
        if self.error:
            raise self.error
        payload = [] if request.resource == "activities_page" else None
        return FetchResult(request, payload, NOW.isoformat(), 200)


def covered(plan, resource):
    return [
        day
        for request in plan
        if request.resource == resource
        for day in dates(
            date.fromisoformat(request.start_date), date.fromisoformat(request.end_date)
        )
    ]


@pytest.mark.parametrize("n", [1, 7, 31])
@pytest.mark.parametrize(
    "now",
    [
        NOW,
        datetime(2027, 1, 1, 0, tzinfo=UTC),
        datetime(2026, 12, 31, 15, 59, tzinfo=UTC),
        datetime(2026, 12, 31, 16, tzinfo=UTC),
    ],
)
def test_strict_window_and_load_baselines_without_creating_files(settings, n, now):
    settings.garmin_lookback_days = n
    plan, metadata = prepare_daily(settings, now=now)
    end = now.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")).date()
    first = end - timedelta(days=n - 1)
    for resource in ("sleep", "hrv", "spo2", "body_battery", "wellness_activities"):
        assert covered(plan, resource) == dates(first, end)
    for resource in ("sleep_summaries", "hrv_summaries"):
        assert covered(plan, resource) == dates(first, end - timedelta(days=1))
    assert covered(plan, "heart_rate") == dates(first - timedelta(days=1), end)
    baseline = set(covered(plan, "activities"))
    for night in covered(plan, "sleep"):
        assert set(dates(night - timedelta(days=31), night - timedelta(days=1))) <= baseline
    assert end not in baseline
    assert metadata["detail_days"] == n
    assert len({(r.resource, r.key) for r in plan}) == len(plan)
    assert not settings.data_dir.exists()


def test_schedule_shanghai_nine_and_year_boundary():
    assert schedule(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))["next_planned_at"] == (
        "2027-01-01T09:00:00+08:00"
    )
    assert schedule(NOW)["next_planned_at"] == "2026-03-02T09:00:00+08:00"
    assert schedule(NOW - timedelta(seconds=1))["next_planned_at"] == "2026-03-01T09:00:00+08:00"
    with pytest.raises(ValueError, match="timezone_required"):
        schedule(datetime(2026, 3, 1))


def test_completed_empty_run_is_idempotent_including_explicit_retry(settings):
    gateway = Gateway()
    result = run_daily(settings, now=NOW, gateway=gateway)
    assert result["status"] == "completed"
    assert result["analysis"] == "disabled"
    assert all(row["sleep"] == "missing" for row in result["observations"])
    assert result["run"]["request_counts"]["http"] == len(gateway.calls)
    for retry in (False, True):
        unused = Gateway()
        repeated = run_daily(settings, now=NOW, retry=retry, gateway=unused)
        assert repeated["status"] == "completed"
        assert repeated["action"] in {"skipped_existing", "retry_not_allowed"}
        assert unused.calls == []
    assert len(read_state(settings.data_dir)["days"]) == 1


def test_partial_requires_explicit_retry_and_no_outer_retry_loop(settings):
    gateway = Gateway(GarminError("network_retry_exhausted"))
    failed = run_daily(settings, now=NOW, gateway=gateway)
    assert failed["status"] == "partial"
    assert len(gateway.calls) == failed["run"]["request_counts"]["planned"]
    unused = Gateway()
    assert run_daily(settings, now=NOW, gateway=unused)["action"] == "skipped_existing"
    assert not unused.calls
    retry = run_daily(settings, now=NOW, retry=True, gateway=Gateway())
    assert retry["status"] == "completed"
    assert retry["run"]["attempt"] == 2


def test_shared_lock_blocks_daily_and_manual_without_consuming_day(settings):
    unused = Gateway()
    with collection_lock(settings.data_dir):
        result = run_daily(settings, now=NOW, gateway=unused)
        assert result["status"] == "blocked"
        assert result["error_code"] == "collection_already_running"
        with pytest.raises(Exception, match="collection_already_running"):
            collect(settings, NOW.date(), gateway=unused)
    assert not unused.calls
    assert not (settings.data_dir / STATE_FILE).exists()
    assert run_daily(settings, now=NOW, gateway=Gateway())["status"] == "completed"


def test_keyboard_interrupt_preserves_safe_terminal_state(settings):
    with pytest.raises(KeyboardInterrupt):
        run_daily(settings, now=NOW, gateway=Gateway(interrupt=True))
    record = read_state(settings.data_dir)["days"][0]
    assert record["status"] == "interrupted"
    assert run_daily(settings, now=NOW, gateway=Gateway())["status"] == "interrupted"
    assert run_daily(settings, now=NOW, retry=True, gateway=Gateway())["status"] == "completed"


def test_orphan_before_database_start_is_reconciled_without_network(settings):
    run_daily(settings, now=NOW, gateway=Gateway())
    state = read_state(settings.data_dir)
    state["days"][0].update(status="running", finished_at=None, phase="preparing", run_id=None)
    write_state(settings.data_dir, state)
    before = (settings.data_dir / STATE_FILE).read_bytes()
    assert daily_status(settings, now=NOW)["latest"]["status"] == "interrupted"
    assert (settings.data_dir / STATE_FILE).read_bytes() == before
    unused = Gateway()
    result = run_daily(settings, now=NOW, gateway=unused)
    assert result["status"] == "interrupted"
    assert not unused.calls
    assert read_state(settings.data_dir)["days"][0]["status"] == "interrupted"


def test_long_outage_does_not_expand_window_and_day_history_is_bounded(settings):
    first = run_daily(settings, now=NOW, gateway=Gateway())
    state = read_state(settings.data_dir)
    template = state["days"][0]
    # Thirty-one prior independent days, without running a thousand synthetic requests.
    state["days"] = []
    for offset in range(MAX_DAYS):
        row = json.loads(json.dumps(template))
        moment = NOW + timedelta(days=offset)
        row.update(
            day=moment.date().isoformat(),
            started_at=moment.isoformat(),
            finished_at=moment.isoformat(),
        )
        row["window"] = {
            "start_date": (moment.date() - timedelta(days=6)).isoformat(),
            "end_date": moment.date().isoformat(),
        }
        state["days"].append(row)
    write_state(settings.data_dir, state)
    late = run_daily(settings, now=NOW + timedelta(days=120), gateway=Gateway())
    assert late["run"]["request_counts"]["planned"] == first["run"]["request_counts"]["planned"]
    assert len(read_state(settings.data_dir)["days"]) == MAX_DAYS
    assert len(late["observations"]) == 7


@pytest.mark.parametrize("mutation", ["invalid", "secret_field", "public_mode", "symlink"])
def test_unsafe_state_fails_closed_and_never_leaks(settings, tmp_path, mutation):
    run_daily(settings, now=NOW, gateway=Gateway())
    path = settings.data_dir / STATE_FILE
    if mutation == "invalid":
        path.write_text('{"secret":"synthetic-sensitive-text"')
    elif mutation == "secret_field":
        value = json.loads(path.read_text())
        value["secret"] = "synthetic-sensitive-text"
        path.write_text(json.dumps(value))
    elif mutation == "public_mode":
        path.chmod(0o644)
    else:
        target = tmp_path / "outside.json"
        path.rename(target)
        path.symlink_to(target)
    before = path.read_bytes()
    gateway = Gateway()
    result = run_daily(settings, now=NOW + timedelta(days=1), gateway=gateway)
    status = daily_status(settings, now=NOW)
    assert result["status"] == "blocked"
    assert status["state"] == "unavailable"
    assert "synthetic-sensitive-text" not in json.dumps((result, status))
    assert not gateway.calls
    assert path.read_bytes() == before


def test_future_ledger_fails_closed_on_clock_regression(settings):
    run_daily(settings, now=NOW, gateway=Gateway())
    assert run_daily(settings, now=NOW - timedelta(days=1), gateway=Gateway())["error_code"] == (
        "daily_clock_regressed"
    )


def test_status_missing_directory_is_readonly_and_has_no_health_or_run_id(settings):
    status = daily_status(settings, now=NOW)
    assert status["state"] == "not_run"
    assert not settings.data_dir.exists()
    run_daily(settings, now=NOW, gateway=Gateway())
    status = daily_status(settings, now=NOW)
    assert "run_id" not in json.dumps(status)
    assert status["schedule"]["installation_state"] == "unknown"
    assert status["latest"]["status"] == "completed"


@pytest.mark.parametrize("mutation", ["corrupt", "public_mode", "symlink"])
def test_unsafe_shared_cooldown_blocks_daily_and_manual_without_network(
    settings, tmp_path, mutation
):
    with collection_lock(settings.data_dir):
        defer_until(settings.data_dir, (datetime.now(UTC) + timedelta(hours=1)).isoformat())
    path = settings.data_dir / COOLDOWN_FILE
    if mutation == "corrupt":
        path.write_text('{"synthetic-sensitive-text":')
    elif mutation == "public_mode":
        path.chmod(0o644)
    else:
        outside = tmp_path / "outside-cooldown.json"
        path.rename(outside)
        path.symlink_to(outside)
    before = path.read_bytes()
    gateway = Gateway()
    result = run_daily(settings, now=NOW, gateway=gateway)
    assert result["status"] == "blocked"
    assert result["error_code"] == "daily_state_unavailable"
    with pytest.raises(CollectionError, match="^daily_state_unavailable$"):
        collect(settings, NOW.date(), gateway=gateway)
    assert not gateway.calls
    assert path.read_bytes() == before
    assert "synthetic-sensitive-text" not in json.dumps(result)
    with Repository(settings.data_dir) as repository:
        assert repository.counts()["runs"] == 0


def test_gateway_close_failure_preserves_completed_collection_and_daily_state(
    settings, monkeypatch
):
    class CleanupFailure(Gateway):
        def close(self):
            raise RuntimeError("synthetic-sensitive-cleanup-error")

    gateway = CleanupFailure()
    monkeypatch.setattr("sleepscape.services.collection.GarminGateway", lambda _: gateway)
    result = run_daily(settings, now=NOW)
    assert result["status"] == "completed"
    assert gateway.calls
    with Repository(settings.data_dir) as repository:
        latest = repository.latest_run()
    assert latest["status"] == "completed"
    assert latest["summary"]["cleanup_error"] == "gateway_close_failed"
    status = daily_status(settings, now=NOW)
    assert status["latest"]["status"] == "completed"
    assert "synthetic-sensitive-cleanup-error" not in json.dumps((result, status, latest))
