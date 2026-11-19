"""Explicit daily entry points and the single deployment timer contract."""

import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from sleepscape import cli
from sleepscape.config import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.upper().startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("count", [1, 7, 31])
def test_daily_window_loads_from_environment(count, monkeypatch):
    monkeypatch.setenv("SLEEPSCAPE_GARMIN_LOOKBACK_DAYS", str(count))
    assert Settings(_env_file=None).garmin_lookback_days == count


@pytest.mark.parametrize("value", ["0", "-7", "32", "7.5", "many"])
def test_invalid_daily_window_is_rejected(value, monkeypatch):
    monkeypatch.setenv("SLEEPSCAPE_GARMIN_LOOKBACK_DAYS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("args", [["daily-run", "--plan-only"], ["daily-status"]])
def test_offline_daily_commands_do_not_call_or_create_files(args, monkeypatch, capsys, tmp_path):
    from sleepscape.services import daily

    monkeypatch.setattr(daily, "run_daily", lambda *a, **kw: pytest.fail("unexpected collection"))
    monkeypatch.setattr(cli, "verify", lambda *a: pytest.fail("unexpected verification"))
    monkeypatch.setattr(sys, "argv", ["sleepscape", *args])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output
    assert not list(tmp_path.iterdir())
    if "--plan-only" in args:
        assert output["status"] == "planned"
        assert output["lookback_days"] == 7
        assert output["analysis"] == "automatic_disabled"
        assert "requests" not in output


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize(
    "status", ["completed", "partial", "failed", "interrupted", "deferred", "blocked"]
)
def test_daily_exit_status_and_explicit_retry(status, retry, monkeypatch, capsys):
    from sleepscape.services import daily

    calls = []

    def execute(settings, **options):
        calls.append(options)
        return {"status": status, "action": "executed", "analysis": "skipped_deferred"}

    monkeypatch.setattr(daily, "run_daily", execute)
    monkeypatch.setattr(cli, "verify", lambda *a: pytest.fail("unexpected verification"))
    monkeypatch.setattr(sys, "argv", ["sleepscape", "daily-run", *(["--retry"] if retry else [])])
    if status == "completed":
        cli.main()
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
    assert calls == [{"retry": retry}]
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_retry_and_plan_only_cannot_be_combined(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sleepscape", "daily-run", "--retry", "--plan-only"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_deployment_keeps_one_nine_am_timer_with_daily_entry():
    service = (ROOT / "deploy/garmin-sync.service").read_text()
    timer = (ROOT / "deploy/garmin-sync.timer").read_text()
    assert "ExecStart=/opt/sleepscape/.venv/bin/sleepscape daily-run\n" in service
    assert "OnCalendar=*-*-* 09:00:00 Asia/Shanghai\n" in timer
    assert "Persistent=true\n" in timer
    assert "Unit=garmin-sync.service\n" in timer
    daily_units = [
        path
        for path in (ROOT / "deploy").glob("*.service")
        if "sleepscape daily-run" in path.read_text()
    ]
    assert daily_units == [ROOT / "deploy/garmin-sync.service"]
