"""Explicit P2 command wiring remains separate from dashboard and model reads."""

import json
import sys

import pytest

from sleepscape import cli
from sleepscape.services import environment_index
from sleepscape.storage.environment import EnvironmentError


@pytest.mark.parametrize("rebuild", [False, True])
def test_explicit_index_command_passes_rebuild_flag(tmp_path, monkeypatch, capsys, rebuild):
    monkeypatch.chdir(tmp_path)
    argv = ["sleepscape", "environment-index"] + (["--rebuild"] if rebuild else [])
    monkeypatch.setattr(sys, "argv", argv)
    calls = []

    def prepare(settings, force=False):
        calls.append(force)
        return {"status": "ready"}

    monkeypatch.setattr(environment_index, "prepare_environment_index", prepare)
    cli.main()
    assert calls == [rebuild]
    assert json.loads(capsys.readouterr().out) == {"status": "ready"}


@pytest.mark.parametrize("verified", [False, True])
def test_index_verification_exit_code(tmp_path, monkeypatch, capsys, verified):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["sleepscape", "environment-index-verify"])
    monkeypatch.setattr(
        environment_index,
        "verify_environment_index",
        lambda _: {"status": "verified" if verified else "failed"},
    )
    if verified:
        cli.main()
    else:
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == ("verified" if verified else "failed")


def test_index_command_error_is_safe(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["sleepscape", "environment-index"])

    def failed(*args, **kwargs):
        raise EnvironmentError("environment_index_source_unavailable")

    monkeypatch.setattr(environment_index, "prepare_environment_index", failed)
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "environment_operation_failed",
        "code": "environment_index_source_unavailable",
    }
