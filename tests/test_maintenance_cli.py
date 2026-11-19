"""Maintenance commands are explicit; status and plans never mutate runtime state."""

import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from sleepscape import cli
from sleepscape.config import Settings
from sleepscape.services import maintenance

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("args", [["maintenance-status"], ["maintenance-run", "--plan-only"]])
def test_status_and_preview_are_readonly(args, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(maintenance, "run_maintenance", lambda *a, **k: pytest.fail("mutation"))
    monkeypatch.setattr(sys, "argv", ["sleepscape", *args])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["schedule"]["time"] == "04:00"
    assert result["offsite"]["status"] == "offsite_not_configured"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "interrupted", "blocked"])
@pytest.mark.parametrize("retry", [False, True])
def test_execution_failure_has_nonzero_exit_and_passes_retry(status, retry, monkeypatch, capsys):
    calls = []

    def run(settings, **options):
        calls.append(options)
        return {"status": status}

    monkeypatch.setattr(maintenance, "run_maintenance", run)
    monkeypatch.setattr(cli, "verify", lambda *a: pytest.fail("unrelated Garmin verification"))
    monkeypatch.setattr(
        sys, "argv", ["sleepscape", "maintenance-run", *(["--retry"] if retry else [])]
    )
    if status == "completed":
        cli.main()
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
    assert calls == [{"retry": retry}]
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_restore_check_passes_only_identifier(monkeypatch, capsys):
    calls = []

    def restore(settings, backup_id):
        calls.append(backup_id)
        return {"status": "verified"}

    monkeypatch.setattr(maintenance, "check_bundle_restore", restore)
    monkeypatch.setattr(sys, "argv", ["sleepscape", "maintenance-restore-check", "a" * 32])
    cli.main()
    assert calls == ["a" * 32]
    assert json.loads(capsys.readouterr().out)["status"] == "verified"


def test_retry_and_preview_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sleepscape", "maintenance-run", "--retry", "--plan-only"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_restore_unknown_backup_returns_safe_failure(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["sleepscape", "maintenance-restore-check", "a" * 32])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "failed", "error_code": "maintenance_bundle_missing"}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "name,value",
    [
        ("KEEP_COUNT", "0"),
        ("MAX_BYTES", "0"),
        ("EXPORT_MODE", "auto"),
        ("EXPORT_PORT", "0"),
        ("EXPORT_TIMEOUT_SECONDS", "0"),
        ("EXPORT_DAILY_KEEP", "0"),
    ],
)
def test_config_rejects_invalid_limits(name, value, monkeypatch):
    monkeypatch.setenv("SLEEPSCAPE_MAINTENANCE_" + name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_maintenance_timer_is_separate_and_not_another_daily_collector():
    service = (ROOT / "deploy/maintenance.service").read_text()
    timer = (ROOT / "deploy/maintenance.timer").read_text()
    daily = (ROOT / "deploy/garmin-sync.timer").read_text()
    assert "ExecStart=/opt/sleepscape/.venv/bin/sleepscape maintenance-run\n" in service
    assert "OnCalendar=*-*-* 04:00:00 Asia/Shanghai\n" in timer
    assert "Persistent=true\n" in timer
    assert "Unit=maintenance.service\n" in timer
    assert "OnCalendar=*-*-* 09:00:00 Asia/Shanghai\n" in daily
