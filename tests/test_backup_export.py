"""Synthetic offsite transport checks; no real destination, network, keys or health data."""

import hashlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from sleepscape.adapters import backup_export as export
from sleepscape.adapters import backup_export_worker as worker
from sleepscape.config import Settings
from sleepscape.domain.retention import select_offsite


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)
    root = tmp_path.resolve()
    destination = root / "destination"
    destination.mkdir(mode=0o700)
    return Settings(
        _env_file=None,
        data_dir=root / "data",
        maintenance_export_mode="path",
        maintenance_export_path=destination,
    )


def bundle(settings, *, day=1, value=b"synthetic private backup", identity=None):
    identity = identity or uuid4().hex
    path = settings.data_dir / "backups" / "managed" / identity
    path.mkdir(parents=True, mode=0o700)
    database = path / "sleepscape.sqlite3"
    database.write_bytes(value)
    database.chmod(0o600)
    payload = {
        "schema_version": 1,
        "backup_id": identity,
        "created_at": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)).isoformat(),
        "files": {
            "sleepscape.sqlite3": {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
        },
        "counts": {},
        "runtime_presence": {
            "model-runtime.json": False,
            "daily-runtime.json": False,
            "collection-cooldown.json": False,
        },
    }
    worker.write_json(path / "manifest.json", payload)
    return path


def remote(settings):
    return settings.maintenance_export_path / worker.NAMESPACE


def remote_state(settings):
    return worker.read_json(remote(settings) / "manifest.json")


def test_disabled_export_reads_no_source_and_creates_nothing(settings):
    settings.maintenance_export_mode = "disabled"
    assert export.export_bundle(settings, settings.data_dir / "absent") == {
        "status": "offsite_not_configured",
        "code": None,
    }
    assert not settings.data_dir.exists()
    assert not remote(settings).exists()


def test_path_export_verifies_bytes_is_idempotent_and_does_not_claim_offsite(settings):
    source = bundle(settings)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    first = export.export_bundle(settings, source)
    second = export.export_bundle(settings, source)
    assert first == second
    assert first["status"] == "verified"
    assert first["location_state"] == "location_unverified"
    assert first["transit_encrypted"] is False
    assert first["at_rest_encryption"] == "not_verified"
    assert first["retained_count"] == 1
    target = remote(settings) / "bundles" / source.name
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in target.iterdir())
    assert "synthetic private" not in json.dumps(first)
    assert str(settings.maintenance_export_path) not in json.dumps(first)


def test_real_managed_sqlite_bundle_contract_roundtrips_transport(settings):
    from sleepscape.services.backup_bundle import create_bundle, validate_bundle
    from sleepscape.storage.repository import Repository

    with Repository(settings.data_dir):
        pass
    identity = uuid4().hex
    create_bundle(settings, identity, datetime(2026, 1, 1, tzinfo=UTC))
    source = settings.data_dir / "backups" / "managed" / identity
    assert export.export_bundle(settings, source)["status"] == "verified"
    target = remote(settings) / "bundles" / identity
    assert validate_bundle(target, restore=True) == validate_bundle(source, restore=True)


@pytest.mark.parametrize("mode", ["path", "ssh"])
def test_full_month_model_ledger_roundtrips_both_transports(settings, tmp_path, monkeypatch, mode):
    from test_backup_bundle import large_model_runtime

    from sleepscape.services.backup_bundle import create_bundle, validate_bundle
    from sleepscape.storage import model_runtime
    from sleepscape.storage.repository import Repository

    if mode == "ssh":
        ssh_settings(settings, tmp_path)
        original = subprocess.Popen

        def local_remote(command, **kwargs):
            # Exercise the remote worker protocol locally; never open a network connection.
            parsed = shlex.split(command[-1])
            return original([sys.executable, *parsed[1:]], **kwargs)

        monkeypatch.setattr(export.subprocess, "Popen", local_remote)
    with Repository(settings.data_dir):
        pass
    original_bytes = large_model_runtime(settings)
    identity = uuid4().hex
    create_bundle(settings, identity, datetime(2026, 11, 1, 1, tzinfo=UTC))
    source = settings.data_dir / "backups" / "managed" / identity
    assert export.export_bundle(settings, source)["status"] == "verified"
    target = remote(settings) / "bundles" / identity
    assert validate_bundle(target, restore=True) == validate_bundle(source, restore=True)
    assert (target / model_runtime.STATE_FILE).read_bytes() == original_bytes
    assert worker.MAX_META == 128 * 1024  # Only control metadata uses this smaller bound.


@pytest.mark.parametrize("mutation", ["corrupt", "symlink", "public", "extra"])
def test_invalid_source_never_changes_existing_verified_destination(settings, mutation):
    good = bundle(settings)
    export.export_bundle(settings, good)
    before = (remote(settings) / "manifest.json").read_bytes()
    bad = bundle(settings, day=2)
    database = bad / "sleepscape.sqlite3"
    if mutation == "corrupt":
        database.write_bytes(b"unexpected source change")
    elif mutation == "symlink":
        database.unlink()
        database.symlink_to(good / "sleepscape.sqlite3")
    elif mutation == "public":
        database.chmod(0o644)
    else:
        (bad / "unknown.txt").write_text("not managed")
    with pytest.raises(export.BackupExportError):
        export.export_bundle(settings, bad)
    assert (remote(settings) / "manifest.json").read_bytes() == before
    assert (remote(settings) / "bundles" / good.name).is_dir()


@pytest.mark.parametrize("mutation", ["missing", "relative", "public", "symlink", "inside_source"])
def test_destination_must_be_explicit_existing_private_directory(settings, mutation, tmp_path):
    source = bundle(settings)
    if mutation == "missing":
        settings.maintenance_export_path = tmp_path.resolve() / "missing"
    elif mutation == "relative":
        settings.maintenance_export_path = Path("relative")
    elif mutation == "public":
        settings.maintenance_export_path.chmod(0o755)
    elif mutation == "symlink":
        path = tmp_path.resolve() / "alias"
        path.symlink_to(settings.maintenance_export_path, target_is_directory=True)
        settings.maintenance_export_path = path
    else:
        settings.maintenance_export_path = source
    with pytest.raises(export.BackupExportError):
        export.export_bundle(settings, source)
    assert not (source / worker.NAMESPACE).exists()


def test_unknown_namespace_and_files_are_never_adopted_or_deleted(settings):
    namespace = remote(settings)
    namespace.mkdir(mode=0o700)
    (namespace / "bundles").mkdir(mode=0o700)
    unknown = namespace / "precious.txt"
    unknown.write_text("manual file")
    with pytest.raises(export.BackupExportError):
        export.export_bundle(settings, bundle(settings))
    assert unknown.read_text() == "manual file"


def test_daily_weekly_retention_and_byte_limit_only_delete_registered_copies(settings):
    settings.maintenance_export_daily_keep = 2
    settings.maintenance_export_weekly_keep = 2
    all_entries = []
    for day in [1, 8, 15, 16, 17]:
        source = bundle(settings, day=day)
        result = export.export_bundle(settings, source)
        manifest, descriptors = worker.inspect_bundle(source)
        all_entries.append(
            {
                "id": source.name,
                "created_at": manifest["created_at"],
                "size_bytes": sum(item["size"] for item in descriptors.values()),
            }
        )
    expected = select_offsite(all_entries, daily_count=2, weekly_count=2)
    assert {item["id"] for item in remote_state(settings)["entries"]} == set(expected["keep"])
    assert result["retained_count"] <= 4
    unknown = remote(settings) / "bundles" / "unmanaged-folder"
    unknown.mkdir(mode=0o700)
    (unknown / "private").write_text("not ours")
    settings.maintenance_export_max_bytes = 1
    newest = bundle(settings, day=20)
    result = export.export_bundle(settings, newest)
    assert result["retained_count"] == 1
    assert result["over_budget"] is True
    assert (remote(settings) / "bundles" / newest.name).exists()
    assert (unknown / "private").read_text() == "not ours"


def test_corrupt_old_destination_prevents_cleanup_and_keeps_new_verified_backup(settings):
    settings.maintenance_export_daily_keep = 1
    settings.maintenance_export_weekly_keep = 0
    old = bundle(settings)
    export.export_bundle(settings, old)
    damaged = remote(settings) / "bundles" / old.name / "sleepscape.sqlite3"
    damaged.write_bytes(b"damage")
    new = bundle(settings, day=2)
    with pytest.raises(export.BackupExportError, match="export_digest_mismatch"):
        export.export_bundle(settings, new)
    assert damaged.read_bytes() == b"damage"
    assert (remote(settings) / "bundles" / new.name).exists()
    assert len(remote_state(settings)["entries"]) == 2


def test_partial_stream_or_digest_failure_does_not_publish_or_delete(settings):
    first = bundle(settings)
    export.export_bundle(settings, first)
    before = (remote(settings) / "manifest.json").read_bytes()
    next_source = bundle(settings, day=2)
    manifest, descriptors = worker.inspect_bundle(next_source)
    request = {
        "destination": str(settings.maintenance_export_path),
        "backup_id": next_source.name,
        "created_at": manifest["created_at"],
        "files": descriptors,
    }
    with pytest.raises(worker.ExportFailure, match="export_transfer_incomplete"):
        worker.upload(request, io.BytesIO(b""))
    assert (remote(settings) / "manifest.json").read_bytes() == before
    assert not list(remote(settings).glob(".staging-*"))


def test_journaled_partial_deletion_can_resume_without_removing_unknown_files(settings):
    old = bundle(settings)
    new = bundle(settings, day=2)
    export.export_bundle(settings, old)
    export.export_bundle(settings, new)
    namespace = remote(settings)
    state = remote_state(settings)
    state["deleting"] = [old.name]
    worker.write_json(namespace / "manifest.json", state)
    # Simulate death after deleting one verified file but before removing the manifest.
    (namespace / "bundles" / old.name / "sleepscape.sqlite3").unlink()
    result = export.export_bundle(settings, new)
    assert result["retained_count"] == 1
    assert not (namespace / "bundles" / old.name).exists()
    assert (namespace / "bundles" / new.name).exists()


def test_publish_interruption_reconciles_only_exact_immutable_source(settings):
    source = bundle(settings)
    export.export_bundle(settings, source)
    state = remote_state(settings)
    state["entries"] = []
    worker.write_json(remote(settings) / "manifest.json", state)
    assert export.export_bundle(settings, source)["retained_count"] == 1
    state["entries"] = []
    worker.write_json(remote(settings) / "manifest.json", state)
    target = remote(settings) / "bundles" / source.name / "sleepscape.sqlite3"
    target.write_bytes(b"unregistered changed content")
    with pytest.raises(export.BackupExportError, match="export_digest_mismatch"):
        export.export_bundle(settings, source)
    assert target.read_bytes() == b"unregistered changed content"


def test_remote_manifest_revision_mismatch_prevents_retention_delete(settings):
    old = bundle(settings)
    new = bundle(settings, day=2)
    export.export_bundle(settings, old)
    export.export_bundle(settings, new)
    with pytest.raises(worker.ExportFailure, match="export_manifest_changed"):
        worker.prune(
            {
                "destination": str(settings.maintenance_export_path),
                "backup_id": new.name,
                "delete": [old.name],
                "manifest_hash": "0" * 64,
            }
        )
    assert (remote(settings) / "bundles" / old.name).exists()


def ssh_settings(settings, tmp_path):
    secret = tmp_path.resolve() / "ssh-private"
    secret.mkdir(mode=0o700)
    for name in ("identity", "known_hosts"):
        path = secret / name
        path.write_text("synthetic metadata only")
        path.chmod(0o600)
    settings.maintenance_export_mode = "ssh"
    settings.maintenance_export_host = "backup.example.invalid"
    settings.maintenance_export_user = "backup_user"
    settings.maintenance_export_identity_file = secret / "identity"
    settings.maintenance_export_known_hosts_file = secret / "known_hosts"


def test_ssh_uses_fixed_command_strict_trust_and_streams_remote_digest(
    settings, tmp_path, monkeypatch
):
    ssh_settings(settings, tmp_path)
    special = tmp_path.resolve() / "target with spaces;$(do-not-execute)"
    special.mkdir(mode=0o700)
    settings.maintenance_export_path = special
    original = subprocess.Popen
    calls = []

    def local_remote(command, **kwargs):
        calls.append(command)
        assert command[0] == "ssh"
        assert str(special) not in " ".join(command)
        # Run the actual fixed remote Python worker locally, never call SSH/network.
        parsed = shlex.split(command[-1])
        assert parsed[:4] == ["python3", "-I", "-B", "-c"]
        return original([sys.executable, *parsed[1:]], **kwargs)

    monkeypatch.setattr(export.subprocess, "Popen", local_remote)
    result = export.export_bundle(settings, bundle(settings))
    assert result["status"] == "verified"
    assert result["transit_encrypted"] is True
    assert result["location_state"] == "remote_host"
    assert result["at_rest_encryption"] == "not_verified"
    assert "StrictHostKeyChecking=yes" in calls[0]
    assert "BatchMode=yes" in calls[0]
    assert "IdentitiesOnly=yes" in calls[0]
    assert "PasswordAuthentication=no" in calls[0]
    assert calls[0][1:3] == ["-F", "/dev/null"]
    assert settings.maintenance_export_host not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("maintenance_export_host", "-oProxyCommand=unsafe"),
        ("maintenance_export_host", "host; touch nope"),
        ("maintenance_export_user", "name$(unsafe)"),
        ("maintenance_export_known_hosts_file", None),
        ("maintenance_export_identity_file", None),
    ],
)
def test_ssh_bad_configuration_never_starts_process(settings, tmp_path, monkeypatch, field, value):
    ssh_settings(settings, tmp_path)
    setattr(settings, field, value)
    monkeypatch.setattr(export.subprocess, "Popen", lambda *a, **kw: pytest.fail("unexpected SSH"))
    with pytest.raises(export.BackupExportError, match="export_configuration_invalid"):
        export.export_bundle(settings, bundle(settings))


def test_timeout_kills_transport_and_preserves_previous_backup(settings, monkeypatch):
    old = bundle(settings)
    export.export_bundle(settings, old)
    before = (remote(settings) / "manifest.json").read_bytes()
    settings.maintenance_export_timeout_seconds = 1
    monkeypatch.setattr(
        export,
        "_command",
        lambda _: [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
    )
    with pytest.raises(export.BackupExportError, match="export_timeout"):
        export.export_bundle(settings, bundle(settings, day=2))
    assert (remote(settings) / "manifest.json").read_bytes() == before


def test_private_remote_error_text_never_escapes(settings, monkeypatch):
    monkeypatch.setattr(
        export,
        "_command",
        lambda _: [
            sys.executable,
            "-c",
            "import sys; sys.stdin.buffer.read(); "
            'print(\'{"status":"failed","code":"synthetic-private-key-and-path"}\')',
        ],
    )
    with pytest.raises(export.BackupExportError) as exc:
        export.export_bundle(settings, bundle(settings))
    assert str(exc.value) == "export_io_failed"
