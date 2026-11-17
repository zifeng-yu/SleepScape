"""USB capture uses real contracts with synthetic transports; never open hardware."""

import copy
import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sleepscape.config import Settings
from sleepscape.domain.environment import MeasurementProfile
from sleepscape.services import environment_usb as usb
from sleepscape.storage import environment as storage

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("firmware_usb_test", ROOT / "tools/firmware.py")
assert SPEC and SPEC.loader
fw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fw)


@pytest.fixture
def local(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    profile = json.loads((ROOT / "tests/fixtures/environment-profile.json").read_text())
    profile["firmware_version"] = "0.3.0"
    profile["placement_id"] = "bench-usb"
    model = MeasurementProfile.model_validate(profile)
    storage.register_local(settings.data_dir, "synthetic-device", "synthetic-profile", model)
    return settings, model


@pytest.fixture
def batch():
    return json.loads((ROOT / "tests/fixtures/environment-batch.json").read_text())


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Device:
    def __init__(self, settings, batch):
        self.settings = settings
        self.batch = batch
        self.commands = []
        self.sent = False
        self.fail_ack = False
        self.start = {
            "status": "usb_started",
            "firmware_version": "0.3.0",
            "device_id": "synthetic-device",
            "profile_id": "synthetic-profile",
            "boot_id": "current-boot",
            "monotonic_ms": 1000,
        }

    def __call__(self, request):
        self.commands.append(copy.deepcopy(request))
        match request["command"]:
            case "usb_start":
                return self.start
            case "usb_poll":
                if not self.sent:
                    self.sent = True
                    return {"status": "usb_batch", "batch_id": "batch-1", "batch": self.batch}
                return {"status": "usb_idle", "boot_id": "current-boot", "monotonic_ms": 2000}
            case "usb_ack":
                # Prove ACK cannot happen before persisted facts are visible to another reader.
                with sqlite3.connect(self.settings.data_dir / "sleepscape.sqlite3") as connection:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM environment_records"
                    ).fetchone()[0]
                assert count == len(self.batch["records"])
                if self.fail_ack:
                    raise OSError("synthetic lost ACK")
                return {"status": "usb_acked", "batch_id": request["batch_id"]}
            case "usb_stop":
                return {"status": "usb_stopped"}
        raise AssertionError(request)


def run(settings, device, *, duration=4):
    clock = Clock()
    return usb.capture(
        settings,
        "synthetic-device",
        "synthetic-profile",
        duration,
        device,
        monotonic=clock,
        sleep=clock.sleep,
        utc_now=lambda: datetime(2026, 11, 16, tzinfo=UTC),
    )


def test_local_registration_has_no_http_credential_and_is_idempotent(local):
    settings, profile = local
    storage.register_local(settings.data_dir, "synthetic-device", "synthetic-profile", profile)
    assert not storage.local_registration_required(
        settings.data_dir, "synthetic-device", "synthetic-profile", profile
    )
    assert storage.authenticate(settings.data_dir, "local-only:synthetic-device") is None
    assert storage.authenticate(settings.data_dir, "") is None
    assert not settings.environment_ingest_enabled
    changed = profile.model_copy(update={"placement_id": "moved"})
    with pytest.raises(storage.EnvironmentError, match="profile_conflict"):
        storage.register_local(settings.data_dir, "synthetic-device", "synthetic-profile", changed)
    assert storage.status(settings.data_dir)["counts"]["environment_devices"] == 1


def test_local_registration_never_overwrites_cloud_or_revoked_identity(local):
    settings, profile = local
    storage.revoke(settings.data_dir, "synthetic-device")
    with pytest.raises(storage.EnvironmentError, match="usb_device_identity_not_local"):
        storage.register_local(settings.data_dir, "synthetic-device", "other-profile", profile)
    storage.register(settings.data_dir, "cloud-device", "cloud-profile", profile, "real-test-token")
    with pytest.raises(storage.EnvironmentError, match="usb_device_identity_not_local"):
        storage.register_local(settings.data_dir, "cloud-device", "other-profile", profile)
    assert storage.authenticate(settings.data_dir, "real-test-token") == "cloud-device"


def test_capture_commits_before_ack_and_keeps_old_boot_time_identity(local, batch):
    settings, _ = local
    device = Device(settings, batch)
    result = run(settings, device)
    assert result["device_stopped"] is True
    assert result["accepted_records"] == len(batch["records"])
    ack = next(item["ack"] for item in device.commands if item["command"] == "usb_ack")
    assert len(ack["time_anchors"]) == len(batch["time_anchors"])
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        anchors = connection.execute(
            "SELECT boot_id,payload_json FROM environment_time_anchors"
        ).fetchall()
    host = [(boot, json.loads(payload)) for boot, payload in anchors if "host_usb" in payload]
    assert len(host) == 1
    assert host[0][0] == "current-boot"
    assert host[0][1]["uncertainty_ms"] is None
    assert host[0][1]["anchor_id"].startswith("host-usb-")
    assert host[0][0] != batch["boot_id"]


def test_commit_then_lost_ack_replay_is_duplicate(local, batch):
    settings, _ = local
    device = Device(settings, batch)
    device.fail_ack = True
    with pytest.raises(usb.USBCollectionError, match="interrupted_records_retained"):
        run(settings, device)
    assert device.commands[-1]["command"] == "usb_stop"
    result = run(settings, Device(settings, batch))
    assert result["accepted_records"] == 0
    assert result["duplicate_records"] == len(batch["records"])


def test_storage_failure_never_acknowledges_device(local, batch, monkeypatch):
    settings, _ = local
    original = storage.ingest

    def fail_records(data_dir, device_id, value):
        if value.records:
            raise storage.EnvironmentError("synthetic_write_failure")
        return original(data_dir, device_id, value)

    monkeypatch.setattr(storage, "ingest", fail_records)
    device = Device(settings, batch)
    with pytest.raises(storage.EnvironmentError, match="synthetic_write_failure"):
        run(settings, device)
    assert not any(item["command"] == "usb_ack" for item in device.commands)
    assert device.commands[-1]["command"] == "usb_stop"


@pytest.mark.parametrize(
    "change",
    [
        {"firmware_version": "0.2.0"},
        {"device_id": "other"},
        {"profile_id": "other"},
    ],
)
def test_bad_start_identity_or_version_is_stopped(local, batch, change):
    settings, _ = local
    device = Device(settings, batch)
    device.start.update(change)
    with pytest.raises(usb.USBCollectionError, match="identity_or_version"):
        run(settings, device)
    assert device.commands[-1]["command"] == "usb_stop"


def test_lost_start_response_still_attempts_stop(local):
    settings, _ = local
    commands = []

    def exchange(request):
        commands.append(request["command"])
        if request["command"] == "usb_start":
            raise OSError("lost response after device started")
        return {"status": "usb_stopped"}

    with pytest.raises(usb.USBCollectionError, match="interrupted_records_retained"):
        run(settings, exchange)
    assert commands == ["usb_start", "usb_stop"]


def test_unconfirmed_stop_reports_device_may_continue(local, batch):
    settings, _ = local
    device = Device(settings, batch)

    def exchange(request):
        if request["command"] == "usb_stop":
            raise OSError("cable removed")
        return device(request)

    with pytest.raises(usb.USBCollectionError, match="stop_unconfirmed_device_may_still_collect"):
        run(settings, exchange)


def test_prepare_backups_before_new_profile_registration(local, tmp_path, monkeypatch):
    settings, profile = local
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json())
    calls = []
    monkeypatch.setattr(
        usb.backup,
        "create_backup",
        lambda value: (
            calls.append("backup") or {"status": "verified", "backup_path": "backups/test.sqlite3"}
        ),
    )
    monkeypatch.setattr(
        usb.backup, "check_restore", lambda *args: calls.append("restore") or {"status": "verified"}
    )
    usb.prepare(settings, "synthetic-device", "new-profile", profile_path, "bench-usb")
    assert calls == ["backup", "restore"]
    usb.prepare(settings, "synthetic-device", "new-profile", profile_path, "bench-usb")
    assert calls == ["backup", "restore"]


class SerialBytes:
    def __init__(self, content):
        self.content = bytearray(content)
        self.written = []

    def write(self, content):
        self.written.append(content)
        return len(content)

    def flush(self):
        pass

    def read(self, count):
        result = self.content[:count]
        del self.content[:count]
        return bytes(result)


def test_serial_reader_accepts_large_batch_and_ignores_boot_noise():
    response = {"status": "usb_batch", "batch_id": "x", "batch": {"test": "x" * 5000}}
    handle = SerialBytes(b"boot log\ninvalid-json\n" + json.dumps(response).encode() + b"\n")
    assert fw.serial_usb_exchange(handle, {"command": "usb_poll"}) == response


def test_serial_reader_rejects_unbounded_lines():
    with pytest.raises(fw.FirmwareError, match="usb_response_limit"):
        fw.serial_usb_exchange(SerialBytes(b"x" * 70000), {"command": "usb_poll"})


def test_serial_reader_never_echoes_untrusted_device_error():
    secret = "must-not-escape"
    value = {"status": "error", "code": secret}
    with pytest.raises(fw.FirmwareError) as failure:
        fw.serial_usb_exchange(
            SerialBytes(json.dumps(value).encode() + b"\n"), {"command": "usb_poll"}
        )
    assert str(failure.value) == "usb_device_rejected_command"
    assert secret not in str(failure.value)


def test_bridge_skips_late_cancelled_reply_before_stop_confirmation():
    # A real stdio child with synthetic responses verifies pipe framing and reaping.
    script = """
import json,sys
for line in sys.stdin:
    request=json.loads(line)
    print(json.dumps({'request_id':1,'response':{'status':'usb_batch'}}), flush=True)
    print(json.dumps({'request_id':request['request_id'],
                      'response':{'status':'usb_stopped'}}), flush=True)
"""
    bridge = fw.USBBridge.__new__(fw.USBBridge)
    bridge.request_id = 1
    bridge.pending = bytearray()
    bridge.process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        start_new_session=True,
    )
    try:
        assert bridge.exchange({"command": "usb_stop"}) == {"status": "usb_stopped"}
    finally:
        bridge.close()
    assert bridge.process.poll() == 0


def test_bridge_worker_is_isolated_from_terminal_interrupt(monkeypatch):
    options = {}
    monkeypatch.setattr(fw, "idf_python", lambda env: Path("/sdk/python"))
    monkeypatch.setattr(fw.subprocess, "Popen", lambda args, **kwargs: options.update(kwargs))
    fw.USBBridge("/dev/cu.synthetic", {})
    assert options["start_new_session"] is True


def test_missing_channel_is_saved_as_missing_without_zero_fill(local, batch):
    settings, _ = local
    item = batch["records"][0]["metrics"]["temperature_c"]
    item.update(
        state="missing",
        observed_count=0,
        valid_count=0,
        mean=None,
        min=None,
        max=None,
        quality=["missing"],
    )
    run(settings, Device(settings, batch))
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        row = connection.execute(
            "SELECT payload_json FROM environment_records WHERE record_type='minute'"
        ).fetchone()
    saved = json.loads(row[0])["metrics"]["temperature_c"]
    assert saved["state"] == "missing" and saved["mean"] is None
