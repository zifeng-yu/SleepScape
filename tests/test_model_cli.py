"""Explicit model check authorization and safe offline diagnostics."""

import json
import sys

import pytest

from sleepscape import cli
from sleepscape.services import model_health


@pytest.fixture(autouse=True)
def local_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_model_check_requires_live_without_sending(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sleepscape", "model-check"])
    monkeypatch.setattr(model_health, "run_model_check", lambda _: pytest.fail("unexpected call"))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "no request was sent" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["doctor", "model-status"])
def test_offline_commands_do_not_invoke_model(monkeypatch, capsys, command):
    monkeypatch.setattr(sys, "argv", ["sleepscape", command])
    monkeypatch.setattr(model_health, "run_model_check", lambda _: pytest.fail("unexpected call"))
    cli.main()
    response = json.loads(capsys.readouterr().out)
    model = response["model"] if command == "doctor" else response
    assert model["configured"] is False
    assert model["connection_state"] == "not_checked"


@pytest.mark.parametrize("status,exit_code", [("verified", None), ("failed", 1), ("blocked", 1)])
def test_explicit_live_command_propagates_safe_result(monkeypatch, capsys, status, exit_code):
    calls = []

    def check(settings):
        calls.append(settings)
        return {"status": status, "code": None if status == "verified" else "model_unavailable"}

    monkeypatch.setattr(model_health, "run_model_check", check)
    monkeypatch.setattr(sys, "argv", ["sleepscape", "model-check", "--live"])
    if exit_code is None:
        cli.main()
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == exit_code
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["status"] == status
