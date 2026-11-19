"""Completeness commands stay offline and have explicit write/prepare boundaries."""

import json
import os
import sys
from pathlib import Path

import pytest

from sleepscape import cli
from sleepscape.services import readiness
from sleepscape.storage.analysis import AnalysisStorageError


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.upper().startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv(
        "SLEEPSCAPE_LLM_CONFIG_FILE",
        str(Path(__file__).resolve().parents[1] / "configs/models.toml"),
    )


def test_readonly_check_does_not_create_data(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        sys, "argv", ["sleepscape", "analysis-check", "--date", "2026-11-18", "--read-only"]
    )
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "checked" and result["model_calls"] == 0
    assert not result["persisted"] and not list(tmp_path.iterdir())


def test_missing_schema_requires_explicit_prepare(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sleepscape", "analysis-check", "--date", "2026-11-18"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["code"] == "analysis_not_initialized"


def test_prepare_dispatches_only_explicitly(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        readiness,
        "prepare_readiness",
        lambda settings: calls.append(True) or {"status": "initialized"},
    )
    monkeypatch.setattr(sys, "argv", ["sleepscape", "analysis-prepare"])
    cli.main()
    assert calls == [True] and json.loads(capsys.readouterr().out)["status"] == "initialized"


def test_storage_failure_uses_fixed_error_code(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AnalysisStorageError("analysis_payload_invalid")

    monkeypatch.setattr(readiness, "check_readiness", fail)
    monkeypatch.setattr(sys, "argv", ["sleepscape", "analysis-check"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "code": "analysis_payload_invalid",
    }


@pytest.mark.parametrize("value", ["not-a-date", "1999-01-01", "2101-01-01"])
def test_invalid_date_rejected(monkeypatch, value):
    monkeypatch.setattr(
        sys, "argv", ["sleepscape", "analysis-check", "--date", value, "--read-only"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
