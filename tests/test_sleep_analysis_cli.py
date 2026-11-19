"""Explicit report CLI authorization and safe output, using only synthetic SQLite."""

import json
import sys
from datetime import timedelta

import pytest
from test_sleep_analysis_service import DAY, NOW, good_caller
from test_sleep_analysis_service import configured as configured_fixture
from test_sleep_analysis_service import data as data_fixture
from test_sleep_analysis_service import isolate as isolate_fixture
from test_sleep_analysis_service import prepared as prepared_fixture

from sleepscape import cli
from sleepscape.config import Settings
from sleepscape.models.base import ModelError
from sleepscape.services import sleep_analysis as service
from sleepscape.storage import sleep_analysis as store

# Explicitly registered aliases keep these fixtures local to this module.
data = data_fixture
configured = configured_fixture
prepared = prepared_fixture
isolate = isolate_fixture


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["sleepscape", *arguments])
    cli.main()


def test_report_prepare_cli_uses_real_backup_migration_and_never_calls_model(
    configured, monkeypatch, capsys
):
    settings, _ = configured
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    invoke(monkeypatch, "sleep-analysis-prepare")
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "initialized", "schema_version": 1, "backup_verified": True}
    invoke(monkeypatch, "sleep-analysis-prepare")
    assert json.loads(capsys.readouterr().out)["status"] == "already_initialized"
    assert store.read_attempts(settings.data_dir, str(DAY)) == []


def test_missing_live_flag_refuses_before_running_any_analysis(prepared, monkeypatch, capsys):
    settings, _ = prepared
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        service, "run_analysis", lambda *a, **k: pytest.fail("no --live means zero calls")
    )
    with pytest.raises(SystemExit) as stopped:
        invoke(monkeypatch, "sleep-analyze", "--date", str(DAY))
    assert stopped.value.code == 2
    output = capsys.readouterr()
    assert "requires --live" in output.err and output.out == ""
    assert store.read_attempts(settings.data_dir, str(DAY)) == []


def test_live_cli_runs_real_pipeline_and_repeat_does_not_republish(prepared, monkeypatch, capsys):
    settings, _ = prepared
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    original = service.run_analysis
    monkeypatch.setattr(
        service, "run_analysis", lambda s, **kw: original(s, now=NOW, caller=good_caller, **kw)
    )
    invoke(monkeypatch, "sleep-analyze", "--live", "--date", str(DAY))
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "completed" and first["model_calls"] == 1
    assert first["results"][0]["state"] == "complete"
    encoded = json.dumps(first)
    for hidden in (
        "prompt",
        "input_hash",
        "source_fingerprint",
        "synthetic-provider",
        "synthetic-test-key",
        "observations",
        "evidence_ids",
    ):
        assert hidden not in encoded
    original_report = store.read_report(settings.data_dir, str(DAY))
    invoke(monkeypatch, "sleep-analyze", "--live", "--date", str(DAY))
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    assert store.read_report(settings.data_dir, str(DAY)) == original_report


def test_live_model_failure_has_nonzero_exit_and_only_safe_status(prepared, monkeypatch, capsys):
    settings, _ = prepared
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    original = service.run_analysis

    def failed(*args):
        raise ModelError("authentication_failed")

    monkeypatch.setattr(
        service, "run_analysis", lambda s, **kw: original(s, now=NOW, caller=failed, **kw)
    )
    with pytest.raises(SystemExit) as stopped:
        invoke(monkeypatch, "sleep-analyze", "--live", "--date", str(DAY))
    assert stopped.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "partial"
    assert result["results"][0]["code"] == "authentication_failed"
    assert result["model_calls"] == 1
    assert store.read_report(settings.data_dir, str(DAY)) is None


@pytest.mark.parametrize("day", ["bad-date", "1999-01-01", "2026-01-02", "2025-12-25"])
def test_cli_rejects_invalid_or_out_of_window_date_without_calls(
    prepared, monkeypatch, capsys, day
):
    settings, _ = prepared
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    original = service.run_analysis
    monkeypatch.setattr(
        service, "run_analysis", lambda s, **kw: original(s, now=NOW, caller=good_caller, **kw)
    )
    with pytest.raises(SystemExit) as stopped:
        invoke(monkeypatch, "sleep-analyze", "--live", "--date", day)
    assert stopped.value.code == 2
    assert store.read_attempts(settings.data_dir, str(DAY)) == []
    assert capsys.readouterr().out == ""


def test_live_without_date_obeys_same_configured_window(prepared, monkeypatch, capsys):
    settings, _ = prepared
    settings.garmin_lookback_days = 2
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    original = service.run_analysis
    monkeypatch.setattr(
        service, "run_analysis", lambda s, **kw: original(s, now=NOW, caller=good_caller, **kw)
    )
    invoke(monkeypatch, "sleep-analyze", "--live")
    result = json.loads(capsys.readouterr().out)
    assert [row["night_date"] for row in result["results"]] == [
        str(DAY),
        str(DAY - timedelta(days=1)),
    ]
    assert result["model_calls"] == 1


def test_automatic_generation_defaults_off_and_reads_explicit_environment(monkeypatch):
    assert Settings(_env_file=None).llm_automatic_enabled is False
    monkeypatch.setenv("SLEEPSCAPE_LLM_AUTOMATIC_ENABLED", "true")
    assert Settings(_env_file=None).llm_automatic_enabled is True
