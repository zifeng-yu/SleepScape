"""Capacity boundaries and no-ACK/no-call behavior use synthetic data only."""

import errno
import json
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from environment_samples import sample_profile
from fastapi.testclient import TestClient

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.domain.maintenance import GIB, MIB, capacity_permission, capacity_state
from sleepscape.domain.models import DataRequest, FetchResult
from sleepscape.models.base import ModelError
from sleepscape.services.collection import CollectionError, collect
from sleepscape.storage import environment, model_runtime, usage
from sleepscape.web.device_security import INGEST_PATH

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)


def disk(state, free=10 * GIB):
    return {
        "state": state,
        "volumes": [{"role": "data", "total_bytes": 20 * GIB, "free_bytes": free}],
    }


@pytest.mark.parametrize(
    "total,free,state",
    [
        (100 * GIB, 25 * GIB, "normal"),
        (100 * GIB, 25 * GIB - 1, "warning"),
        (100 * GIB, 15 * GIB, "warning"),
        (100 * GIB, 15 * GIB - 1, "restricted"),
        (100 * GIB, 5 * GIB, "restricted"),
        (100 * GIB, 5 * GIB - 1, "critical"),
        (4 * GIB, 2 * GIB, "normal"),
        (4 * GIB, 2 * GIB - 1, "restricted"),
        (GIB, 512 * MIB, "restricted"),
        (GIB, 512 * MIB - 1, "critical"),
        (0, 0, "unknown"),
        (GIB, -1, "unknown"),
        (GIB, GIB + 1, "unknown"),
    ],
)
def test_exact_capacity_thresholds(total, free, state):
    assert capacity_state(total, free) == state


def test_restricted_allows_small_writes_but_blocks_model_and_backups():
    assert capacity_permission(disk("restricted"), "write") is None
    for operation in ("model", "backup"):
        assert capacity_permission(disk("restricted"), operation) == "storage_capacity_restricted"
    assert capacity_permission(disk("normal"), "backup", 8 * GIB) == "backup_space_insufficient"
    assert capacity_permission(disk("normal"), "backup", 7 * GIB) is None


@pytest.mark.parametrize(
    "total,free,required,expected",
    [
        (20 * GIB, 2 * GIB, GIB, None),
        (20 * GIB, 2 * GIB, GIB + 1, "storage_capacity_critical"),
        (4 * GIB, 600 * MIB, 88 * MIB, None),
        (4 * GIB, 600 * MIB, 88 * MIB + 1, "storage_capacity_critical"),
        (4 * GIB, 600 * MIB, 601 * MIB, "storage_capacity_unavailable"),
    ],
)
def test_estimated_writes_respect_projected_critical_floor(total, free, required, expected):
    snapshot = {
        "state": capacity_state(total, free),
        "volumes": [{"role": "data", "total_bytes": total, "free_bytes": free}],
    }
    assert capacity_permission(snapshot, "write", required) == expected


def test_estimated_write_unknown_volume_is_blocked_but_zero_keeps_existing_behavior():
    for volumes in ([], [{"role": "system"}], [{"role": "data", "free_bytes": "unknown"}]):
        snapshot = {"state": "normal", "volumes": volumes}
        assert capacity_permission(snapshot, "write", 1) == "storage_capacity_unavailable"
        assert capacity_permission(snapshot, "write", 0) is None


def test_snapshot_readonly_and_uses_worse_volume(tmp_path, monkeypatch):
    calls = []

    def statvfs(path):
        calls.append(path)
        return SimpleNamespace(
            f_blocks=100 * GIB, f_bavail=4 * GIB if path == Path("/") else 50 * GIB, f_frsize=1
        )

    monkeypatch.setattr(usage.os, "statvfs", statvfs)
    snapshot = usage.disk_snapshot(tmp_path / "absent")
    assert snapshot["state"] == "critical"
    assert len(calls) == 2
    assert str(tmp_path) not in json.dumps(snapshot)
    assert not list(tmp_path.iterdir())


def test_unknown_stat_or_symlink_fails_closed_without_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(usage.os, "statvfs", lambda _: (_ for _ in ()).throw(OSError()))
    with pytest.raises(usage.CapacityError, match="storage_capacity_unavailable"):
        usage.require_capacity(tmp_path / "absent", "write")
    assert not list(tmp_path.iterdir())


@pytest.fixture
def env_config(tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path.resolve() / "data", environment_ingest_enabled=True
    )
    profile = sample_profile()
    token = "synthetic_" + "q" * 43
    environment.register(settings.data_dir, "synthetic-device", "synthetic-profile", profile, token)
    return settings, token


@pytest.mark.parametrize("failure", ["critical", "unknown", "enospc", "sqlite_full"])
def test_environment_full_disk_does_not_ack_or_commit_and_reads_survive(
    env_config, monkeypatch, failure
):
    settings, token = env_config
    if failure in {"critical", "unknown"}:
        monkeypatch.setattr(usage, "disk_snapshot", lambda _: disk(failure))
    else:
        original = environment._append

        def failing_write(*args, **kwargs):
            original(*args, **kwargs)
            if failure == "enospc":
                raise OSError(errno.ENOSPC, "synthetic private error")
            error = sqlite3.OperationalError("synthetic private error")
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise error

        monkeypatch.setattr(environment, "_append", failing_write)
    before = environment.status(settings.data_dir)["counts"]
    with TestClient(
        create_app(settings), base_url="https://localhost", client=("127.0.0.1", 1234)
    ) as web:
        response = web.post(
            INGEST_PATH,
            json=json.loads((FIXTURES / "environment-batch.json").read_text()),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "60"
        assert "records" not in response.json()
        assert "private" not in response.text
        assert web.get("/api/v1/status").status_code == 200
    assert environment.status(settings.data_dir)["counts"] == before


def test_garmin_capacity_blocks_before_network_or_database(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path.resolve() / "data")
    monkeypatch.setattr(usage, "disk_snapshot", lambda _: disk("critical"))
    gateway = SimpleNamespace(fetch=lambda _: pytest.fail("no external request"))
    with pytest.raises(CollectionError, match="storage_capacity_critical"):
        collect(settings, date(2026, 11, 16), gateway=gateway)
    assert not settings.data_dir.exists()


def test_garmin_capacity_rechecked_after_response_before_write(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path.resolve() / "data")
    current = ["normal"]
    monkeypatch.setattr(usage, "disk_snapshot", lambda _: disk(current[0]))

    def fetch(request):
        current[0] = "critical"
        return FetchResult(request, None, "2026-11-16T00:00:00Z", 200)

    result = collect(
        settings,
        date(2026, 11, 16),
        gateway=SimpleNamespace(fetch=fetch),
        plan=[
            DataRequest(
                "sleep", "2026-11-16", "/offline", start_date="2026-11-16", end_date="2026-11-16"
            )
        ],
    )
    assert result["status"] == "failed"
    assert result["stop_reason"] == "storage_capacity_critical"
    assert result["database_counts"]["raw_snapshots"] == 0


def test_capacity_blocks_model_before_budget_is_consumed(tmp_path, monkeypatch):
    target = tmp_path.resolve() / "data"
    monkeypatch.setattr(usage, "disk_snapshot", lambda _: disk("restricted"))
    with model_runtime.session(target) as ledger:
        with pytest.raises(ModelError, match="storage_capacity_restricted"):
            ledger.reserve("a" * 64, datetime(2026, 11, 17, tzinfo=UTC))
        assert ledger.state["attempts"] == []
    assert not (target / "model-runtime.json").exists()
