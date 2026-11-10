"""Current-night sync uses synthetic state only; no account, clock or network writes."""

import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest
import requests

from sleepscape import cli
from sleepscape.adapters.catalog import (
    ALLOWED_GET_PATHS,
    PROVISIONAL_RESOURCES,
    build_plan,
    build_sync_plan,
    daily_request,
)
from sleepscape.adapters.garmin import GarminError
from sleepscape.config import Settings
from sleepscape.domain.calendar import dates
from sleepscape.domain.models import FetchResult
from sleepscape.domain.normalize import normalize
from sleepscape.services.collection import CollectionError, collect, prepare_sync
from sleepscape.storage import Repository

TODAY = date(2026, 3, 1)
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture(autouse=True)
def isolate_account_and_network(monkeypatch):
    for name in tuple(os.environ):
        if name.upper().startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)

    def fail(*args, **kwargs):
        pytest.fail("Synthetic sync tests must not access a real account or network")

    monkeypatch.setattr(requests.Session, "send", fail)
    monkeypatch.setattr("sleepscape.services.collection.GarminGateway", fail)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        timezone="Asia/Shanghai",
        garmin_key_file=tmp_path / "unused-key",
        garmin_token_cache=tmp_path / "unused-token",
    )


def covered(plan, resource):
    return [
        day
        for request in plan
        if request.resource == resource
        for day in dates(
            date.fromisoformat(request.start_date), date.fromisoformat(request.end_date)
        )
    ]


def completed_run(settings, end, *, history=38, mode=None, status="completed"):
    plan = {
        "end_date": end.isoformat(),
        "detail_start": (end - timedelta(days=6)).isoformat(),
        "history_start": (end - timedelta(days=history - 1)).isoformat(),
        "detail_days": 7,
        "history_days": history,
        "timezone": settings.timezone,
    }
    if mode == "sync":
        plan.update(mode="sync", complete_day_through=(end - timedelta(days=1)).isoformat())
    with Repository(settings.data_dir) as repo:
        run = repo.start_run(plan)
        repo.finish_run(run, status, {"status": status})


def test_sync_includes_today_only_for_provisional_resources():
    plan = build_sync_plan(TODAY)
    assert len(plan) == 53
    assert len({(r.resource, r.key) for r in plan}) == len(plan)
    assert {r.resource for r in plan} == {r.resource for r in build_plan(YESTERDAY)}
    for resource in PROVISIONAL_RESOURCES:
        values = covered(plan, resource)
        assert max(values) == TODAY
        assert len(values) == (8 if resource == "heart_rate" else 7)
    for resource in {r.resource for r in plan} - PROVISIONAL_RESOURCES:
        assert max(covered(plan, resource)) == YESTERDAY
    assert covered(plan, "daily") == dates(TODAY - timedelta(days=7), YESTERDAY)
    assert covered(plan, "daily_summaries") == dates(TODAY - timedelta(days=38), YESTERDAY)
    for request in plan:
        assert any(pattern.fullmatch(request.path) for pattern in ALLOWED_GET_PATHS)


@pytest.mark.parametrize("history", [7, 28, 31, 38, 365, 366])
def test_sync_keeps_official_range_limits_and_no_duplicate_partitions(history):
    plan = build_sync_plan(TODAY, detail_days=7, history_days=history)
    for resource, maximum in {
        "daily_summaries": 365,
        "sleep_summaries": 31,
        "hrv_summaries": 28,
        "steps": 28,
        "stress": 28,
        "activities": 7,
    }.items():
        days = covered(plan, resource)
        assert days == dates(TODAY - timedelta(days=history), YESTERDAY)
        assert len(days) == len(set(days))
        for request in (r for r in plan if r.resource == resource):
            length = (
                date.fromisoformat(request.end_date) - date.fromisoformat(request.start_date)
            ).days + 1
            assert length <= maximum


def test_initial_sync_covers_each_nights_previous_day_and_strict_baseline():
    plan = build_sync_plan(TODAY)
    available = set(covered(plan, "activities"))
    for night in covered(plan, "sleep"):
        assert set(dates(night - timedelta(days=31), night - timedelta(days=1))) <= available
    assert TODAY not in available


def test_original_completed_day_plan_is_unchanged():
    plan = build_plan(YESTERDAY)
    assert len(plan) == 51
    assert all(r.end_date <= YESTERDAY.isoformat() for r in plan)
    assert covered(plan, "sleep") == dates(YESTERDAY - timedelta(days=6), YESTERDAY)


@pytest.mark.parametrize("detail,history", [(0, 38), (32, 38), (7, 6), (7, 367)])
def test_invalid_sync_bounds_fail_offline(detail, history):
    with pytest.raises(ValueError):
        build_sync_plan(TODAY, detail, history)


def test_initial_plan_creates_no_files_and_marks_today_provisional(settings):
    plan, metadata = prepare_sync(settings, TODAY)
    assert len(plan) == 53
    assert metadata["sync_strategy"] == "initial"
    assert metadata["history_days"] == 38
    assert metadata["provisional_dates"] == [TODAY.isoformat()]
    assert metadata["complete_day_through"] == YESTERDAY.isoformat()
    assert not settings.data_dir.exists()
    assert not settings.garmin_key_file.exists()


def test_daily_sync_reads_only_run_metadata_and_uses_seven_days(settings):
    completed_run(settings, TODAY - timedelta(days=1), mode="sync")
    path = settings.data_dir / "sleepscape.sqlite3"
    before = path.read_bytes()
    _, metadata = prepare_sync(settings, TODAY)
    assert metadata["sync_strategy"] == "rolling"
    assert metadata["history_days"] == metadata["detail_days"] == 7
    assert path.read_bytes() == before
    with Repository(settings.data_dir) as repo:
        assert repo.counts()["runs"] == 1


def test_weekly_refresh_uses_last_successful_wide_run(settings):
    completed_run(settings, YESTERDAY - timedelta(days=7))
    completed_run(settings, YESTERDAY, history=7)
    _, metadata = prepare_sync(settings, TODAY)
    assert metadata["sync_strategy"] == "weekly"
    assert metadata["history_days"] == 38


def test_failed_runs_do_not_advance_sync_watermark(settings):
    completed_run(settings, TODAY - timedelta(days=11))
    completed_run(settings, YESTERDAY, status="partial")
    plan, metadata = prepare_sync(settings, TODAY)
    assert metadata["missed_complete_days"] == 10
    assert metadata["detail_days"] == 11
    assert min(covered(plan, "sleep")) == TODAY - timedelta(days=10)
    assert metadata["history_days"] == 38


def test_historical_backfill_run_does_not_move_current_watermark_backwards(settings):
    completed_run(settings, YESTERDAY)
    completed_run(settings, TODAY - timedelta(days=90))
    _, metadata = prepare_sync(settings, TODAY)
    assert metadata["missed_complete_days"] == 0
    assert metadata["sync_strategy"] == "rolling"


def test_long_outage_reports_exact_bounded_manual_backfill(settings):
    previous = YESTERDAY - timedelta(days=70)
    completed_run(settings, previous)
    _, metadata = prepare_sync(settings, TODAY)
    gap = metadata["backfill_required"]
    assert gap["start_date"] == (previous + timedelta(days=1)).isoformat()
    assert gap["end_date"] == YESTERDAY.isoformat()
    assert len(gap["commands"]) == 3
    assert "--detail-days 31 --history-days 62" in gap["commands"][0]
    assert "--detail-days 8 --history-days 39" in gap["commands"][-1]
    assert metadata["detail_days"] == 31


def test_explicit_history_override_and_corrupt_metadata_are_safe(settings):
    _, metadata = prepare_sync(settings, TODAY, history_days=62)
    assert metadata["sync_strategy"] == "explicit"
    assert metadata["history_days"] == 62
    settings.data_dir.mkdir()
    (settings.data_dir / "sleepscape.sqlite3").write_bytes(b"synthetic invalid database")
    with pytest.raises(CollectionError, match="sync_state_unavailable"):
        prepare_sync(settings, TODAY)


class SyntheticGateway:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def fetch(self, request):
        self.calls.append(request)
        payload = next(self.responses)
        if isinstance(payload, Exception):
            raise payload
        return FetchResult(request, payload, "2026-03-01T05:00:00Z", 200)


def test_sync_state_is_audit_metadata_not_mutated_measurements(settings):
    _, metadata = prepare_sync(settings, TODAY)
    plan = [daily_request("sleep", TODAY), daily_request("daily", YESTERDAY)]
    payloads = [
        {"calendarDate": TODAY.isoformat(), "sleepTimeSeconds": 100},
        {"calendarDate": YESTERDAY.isoformat(), "totalSteps": 0},
    ]
    result = collect(
        settings,
        TODAY,
        plan=plan,
        metadata=metadata,
        gateway=SyntheticGateway(payloads),
    )
    assert result["status"] == "completed"
    with Repository(settings.data_dir) as repo:
        run = repo.latest_run()
        assert run["plan"]["complete_day_through"] == YESTERDAY.isoformat()
        attempts = repo.list_requests(run["id"])
        assert attempts[0]["metrics"]["provisional_dates"] == [TODAY.isoformat()]
        assert attempts[0]["metrics"]["collection_state"] == "provisional"
        assert attempts[1]["metrics"]["provisional_dates"] == []
        assert attempts[1]["metrics"]["collection_state"] == "completed_day"
        actual = repo.list_records(resource="sleep")[0]["data"]
        assert actual == normalize(plan[0], payloads[0], settings.timezone).records[0]["data"]


def test_sync_keeps_lock_and_retry_after_behavior(settings):
    _, metadata = prepare_sync(settings, TODAY)
    plan = [daily_request("sleep", TODAY)]
    first = SyntheticGateway([GarminError("retry_after_deferred", 429, retry_after_seconds=3600)])
    result = collect(settings, TODAY, plan=plan, metadata=metadata, gateway=first)
    assert result["status"] == "partial"
    assert datetime.fromisoformat(result["retry_not_before"]) > datetime.now(UTC)
    second = SyntheticGateway([])
    with pytest.raises(CollectionError, match="retry_after_wait_still_active"):
        collect(settings, TODAY, plan=plan, metadata=metadata, gateway=second)
    assert second.calls == []


def test_sync_plan_only_cli_is_offline(settings, monkeypatch, capsys):
    class ShanghaiBoundary:
        @staticmethod
        def now(zone):
            return datetime(2026, 2, 28, 16, 30, tzinfo=UTC).astimezone(zone)

    monkeypatch.setattr(cli, "datetime", ShanghaiBoundary)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    collect_mock = Mock(side_effect=AssertionError("plan-only must not collect"))
    monkeypatch.setattr(cli, "collect", collect_mock)
    monkeypatch.setattr(sys, "argv", ["sleepscape", "garmin-sync", "--plan-only"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "plan_only"
    assert result["plan"]["sync_strategy"] == "initial"
    assert result["plan"]["end_date"] == TODAY.isoformat()
    assert result["plan"]["complete_day_through"] == YESTERDAY.isoformat()
    assert result["planned_requests"] == 53
    assert not settings.data_dir.exists()
    collect_mock.assert_not_called()


def test_successful_sync_does_not_fail_for_optional_unknown_baseline(settings, monkeypatch, capsys):
    with Repository(settings.data_dir):
        pass
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "collect", Mock(return_value={"status": "completed"}))
    monkeypatch.setattr(
        cli,
        "verify",
        lambda repo: {
            "status": "usable_with_limitations",
            "load_comparison": {"baseline_mean": None, "change_percent": None},
        },
    )
    monkeypatch.setattr(sys, "argv", ["sleepscape", "garmin-sync"])
    cli.main()
    assert '"status": "usable_with_limitations"' in capsys.readouterr().out


def test_long_outage_cli_refuses_network_and_provides_backfill(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    plan, metadata = prepare_sync(settings, TODAY)
    metadata["backfill_required"] = {"start_date": "2025-12-01", "end_date": "2026-02-28"}
    monkeypatch.setattr(cli, "prepare_sync", lambda *a, **kw: (plan, metadata))
    collect_mock = Mock(side_effect=AssertionError("backfill must be reviewed first"))
    monkeypatch.setattr(cli, "collect", collect_mock)
    monkeypatch.setattr(sys, "argv", ["sleepscape", "garmin-sync"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "backfill_required"
    collect_mock.assert_not_called()


def test_web_password_cli_does_not_print_password(settings, monkeypatch, capsys):
    from sleepscape.web import security

    configure = Mock()
    monkeypatch.setattr(security, "configure_password", configure)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["sleepscape", "web-password"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"status": "configured"}
    configure.assert_called_once_with(settings)
