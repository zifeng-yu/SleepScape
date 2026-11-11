"""Synthetic environment wire, HTTP, transaction, privacy and recovery acceptance."""

import base64
import copy
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sleepscape import cli
from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.services.backup import BackupError, check_restore, create_backup
from sleepscape.services.environment import (
    parse_batch,
    register_device,
    register_profile,
    rotate_device_token,
)
from sleepscape.storage import environment as storage
from sleepscape.storage.repository import Repository
from sleepscape.web.device_security import INGEST_PATH, DeviceGate
from sleepscape.web.security import save_password

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "synthetic_" + "x" * 43


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        pytest.fail("Environment acceptance must never contact Garmin")

    monkeypatch.setattr(requests.Session, "send", forbidden)


@pytest.fixture
def payload():
    return json.loads((FIXTURES / "environment-batch.json").read_bytes())


@pytest.fixture
def profile():
    return MeasurementProfile.model_validate_json(
        (FIXTURES / "environment-profile.json").read_bytes()
    )


@pytest.fixture
def settings(tmp_path, profile):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", environment_ingest_enabled=True)
    storage.register(settings.data_dir, "synthetic-device", "synthetic-profile", profile, TOKEN)
    return settings


def browser(settings, *, scheme="https", peer="127.0.0.1"):
    return TestClient(create_app(settings), base_url=f"{scheme}://localhost", client=(peer, 1234))


def headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def counts(settings):
    return storage.status(settings.data_dir)["counts"]


def record_rows(settings):
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        return connection.execute(
            "SELECT boot_id,record_type,seq,mono_start_ms,payload_json FROM environment_records "
            "ORDER BY boot_id,record_type,seq"
        ).fetchall()


def test_disabled_or_missing_environment_never_initializes_database(tmp_path, payload):
    settings = Settings(_env_file=None, data_dir=tmp_path / "absent")
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 405
    assert storage.status(settings.data_dir)["state"] == "not_initialized"
    assert not settings.data_dir.exists()
    settings.environment_ingest_enabled = True
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 503
    assert not settings.data_dir.exists()


def test_wire_fixture_and_exact_identity_ack(settings, payload):
    parsed = EnvironmentBatch.model_validate(payload)
    assert len(parsed.records) == 1
    with browser(settings) as web:
        first = web.post(INGEST_PATH, json=payload, headers=headers())
        assert first.status_code == 200
        result = first.json()
        assert result["records"] == [{"record_type": "minute", "seq": 0, "status": "accepted"}]
        assert result["time_anchors"] == [{"anchor_id": "0", "status": "accepted"}]
        assert result["device_status"] == {"status_seq": 0, "status": "accepted"}
        for _ in range(10):
            response = web.post(INGEST_PATH, json=payload, headers=headers())
            assert response.status_code == 200
            assert response.json()["records"][0]["status"] == "duplicate"
    values = counts(settings)
    assert values["environment_records"] == 1
    assert values["environment_time_anchors"] == 1
    assert values["environment_device_status"] == 1
    assert values["environment_ingest_batches"] == 11
    assert first.headers["cache-control"] == "no-store"
    assert TOKEN not in first.text


def test_conflicting_batch_is_fully_rolled_back(settings, payload):
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200
        changed = copy.deepcopy(payload)
        changed["records"][0]["metrics"]["temperature_c"]["mean"] = 21.8
        new = copy.deepcopy(changed["records"][0])
        new["seq"] = 1
        changed["records"].insert(0, new)
        response = web.post(INGEST_PATH, json=changed, headers=headers())
        assert response.status_code == 409
        assert response.json() == {"error": "record_conflict"}
    assert counts(settings)["environment_records"] == 1
    assert counts(settings)["environment_ingest_batches"] == 1


@pytest.mark.parametrize("kind", ["anchor", "status"])
def test_anchor_and_status_are_immutable(settings, payload, kind):
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200
        if kind == "anchor":
            payload["time_anchors"][0]["utc_at"] = "2026-01-02T00:00:00Z"
        else:
            payload["device_status"]["queued_records"] = 3
        response = web.post(INGEST_PATH, json=payload, headers=headers())
        assert response.status_code == 409
        assert response.json()["error"] == f"{kind}_conflict"
    assert counts(settings)["environment_ingest_batches"] == 1


def test_later_anchor_does_not_rewrite_unknown_time_records(settings, payload):
    payload["time_anchors"] = []
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200
        before = record_rows(settings)
        anchor_only = {
            "schema_version": 1,
            "device_id": payload["device_id"],
            "boot_id": payload["boot_id"],
            "records": [],
            "time_anchors": [
                {
                    "anchor_id": "later",
                    "monotonic_ms": 120000,
                    "utc_at": "2026-01-01T00:02:00Z",
                    "source": "ntp",
                    "uncertainty_ms": None,
                    "quality": [],
                }
            ],
        }
        assert web.post(INGEST_PATH, json=anchor_only, headers=headers()).status_code == 200
    assert before == record_rows(settings)
    assert counts(settings)["environment_time_anchors"] == 1
    assert "utc_start" not in before[0][-1]


def test_out_of_order_and_new_boot_keep_original_identity(settings, payload):
    payload["device_status"] = None
    payload["time_anchors"] = []
    with browser(settings) as web:
        for boot, seq in [("boot-one", 10), ("boot-one", 1), ("boot-two", 1)]:
            payload["boot_id"] = boot
            payload["records"][0]["seq"] = seq
            response = web.post(INGEST_PATH, json=payload, headers=headers())
            assert response.status_code == 200
            assert response.json()["records"][0]["seq"] == seq
    assert counts(settings)["environment_records"] == 3


def test_device_binding_profile_binding_and_revocation(settings, payload, profile):
    storage.register(settings.data_dir, "other-device", "other-profile", profile, "y" * 43)
    with browser(settings) as web:
        payload["device_id"] = "other-device"
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 403
        payload["device_id"] = "synthetic-device"
        payload["records"][0]["measurement_profile_id"] = "other-profile"
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 422
        storage.revoke(settings.data_dir, "synthetic-device")
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 401
    assert counts(settings)["environment_records"] == 0


def test_https_host_and_exact_route_are_required(settings, payload):
    with browser(settings, scheme="http") as web:
        response = web.post(INGEST_PATH, json=payload, headers=headers())
        assert response.status_code == 403
        assert response.json()["error"] == "https_required"
        assert (
            web.post(
                INGEST_PATH, json=payload, headers={**headers(), "X-Forwarded-Proto": "https"}
            ).status_code
            == 403
        )
    with browser(settings) as web:
        assert (
            web.post(
                INGEST_PATH, json=payload, headers={**headers(), "Host": "evil.invalid"}
            ).status_code
            == 400
        )
        for path in [
            INGEST_PATH + "/",
            INGEST_PATH + "-other",
            "/api/v1/status",
            "/api/v1/ingest%2fenvironment-batches",
        ]:
            assert web.post(
                path, json=payload, headers=headers(), follow_redirects=False
            ).status_code in {404, 405}
        assert (
            web.post(
                INGEST_PATH, json=payload, headers={**headers(), "Sec-Fetch-Site": "cross-site"}
            ).status_code
            == 403
        )
    assert counts(settings)["environment_records"] == 0


def test_ui_password_and_device_token_are_not_interchangeable(settings, payload, tmp_path):
    auth = tmp_path / ".private" / "web.json"
    save_password(auth, "demo", "synthetic-long-password")
    settings.web_auth_file = auth
    basic = "Basic " + base64.b64encode(b"demo:synthetic-long-password").decode()
    with browser(settings, peer="192.0.2.2") as web:
        assert (
            web.post(INGEST_PATH, json=payload, headers={"Authorization": basic}).status_code == 401
        )
        assert web.get("/api/v1/status", headers=headers()).status_code == 401
        assert web.get("/api/v1/status", headers={"Authorization": basic}).status_code == 200
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200
        assert web.post("/api/v1/status", headers={"Authorization": basic}).status_code == 405


@pytest.mark.parametrize("bad_token", ["", "short", "z" * 43])
def test_invalid_authentication_cannot_write(settings, payload, bad_token):
    with browser(settings) as web:
        response = web.post(INGEST_PATH, json=payload, headers=headers(bad_token))
    assert response.status_code == 401
    assert counts(settings)["environment_records"] == 0


def test_content_type_compression_and_both_body_limit_paths(settings, payload):
    with browser(settings) as web:
        assert (
            web.post(
                INGEST_PATH, content=b"{}", headers={**headers(), "Content-Type": "text/plain"}
            ).status_code
            == 415
        )
        assert (
            web.post(
                INGEST_PATH, json=payload, headers={**headers(), "Content-Encoding": "gzip"}
            ).status_code
            == 415
        )
        assert web.post(INGEST_PATH, content=b"x" * 65537, headers=headers()).status_code == 413
        assert (
            web.post(
                INGEST_PATH, content=iter([b"x" * 32768, b"x" * 32769]), headers=headers()
            ).status_code
            == 413
        )
        assert (
            web.post(
                INGEST_PATH, content=b"{}", headers={**headers(), "Content-Length": "invalid"}
            ).status_code
            == 400
        )
    assert counts(settings)["environment_records"] == 0


@pytest.mark.parametrize(
    "bad", [b"{}", b'{"schema_version":1,"schema_version":1}', b"{", b"[]", b"null"]
)
def test_malformed_or_duplicate_json_is_rejected(settings, bad):
    with browser(settings) as web:
        assert web.post(INGEST_PATH, content=bad, headers=headers()).status_code == 422


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p.update(schema_version="1"),
        lambda p: p.update(raw_audio="synthetic_audio"),
        lambda p: p["records"][0].update(seq=True),
        lambda p: p["records"][0].update(seq=2**53),
        lambda p: p["records"][0].update(quality=["invented"]),
        lambda p: p["records"][0]["metrics"]["temperature_c"].update(mean=float("nan")),
        lambda p: p["records"][0]["metrics"]["temperature_c"].update(valid_count=7),
        lambda p: p["records"][0]["metrics"]["temperature_c"].update(unit="lx"),
        lambda p: p["records"][0]["metrics"]["sound_relative"].update(energy_mean_dbfs=1.0),
        lambda p: p["records"][0]["metrics"]["sound_relative"].update(detected_count=0),
        lambda p: p["records"].append(copy.deepcopy(p["records"][0])),
    ],
)
def test_strict_schema_rejects_invalid_data_without_private_errors(settings, payload, mutation):
    mutation(payload)
    body = json.dumps(payload).encode()
    with browser(settings) as web:
        response = web.post(INGEST_PATH, content=body, headers=headers())
        assert response.status_code == 422
        assert response.json() == {"error": "invalid_environment_batch"}
    assert counts(settings)["environment_records"] == 0


def test_per_type_count_cap_is_independent_of_byte_cap(payload):
    payload["records"] = [dict(copy.deepcopy(payload["records"][0]), seq=i) for i in range(61)]
    with pytest.raises(ValidationError):
        EnvironmentBatch.model_validate(payload)
    payload["records"] = payload["records"][:60]
    EnvironmentBatch.model_validate(payload)
    with pytest.raises(storage.EnvironmentError, match="environment_body_too_large"):
        parse_batch(json.dumps(payload).encode())


def test_true_zero_light_missing_channel_and_numeric_audio_floor_are_distinct(settings, payload):
    temperature = payload["records"][0]["metrics"]["temperature_c"]
    temperature.update(
        state="missing",
        observed_count=0,
        valid_count=0,
        mean=None,
        min=None,
        max=None,
        quality=["bus_error"],
    )
    sound = payload["records"][0]["metrics"]["sound_relative"]
    sound.update(
        energy_mean_dbfs=None, max_1s_rms_dbfs=None, quality=["below_numeric_floor", "warming"]
    )
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200
    value = json.loads(record_rows(settings)[0][-1])["metrics"]
    assert value["temperature_c"]["mean"] is None
    assert value["relative_humidity_pct"]["valid_count"] == 6
    assert value["illuminance_lux"]["mean"] == 0
    assert value["sound_relative"]["valid_samples"] == 960000
    assert value["sound_relative"]["energy_mean_dbfs"] is None


def test_event_profile_and_payload_are_validated(settings, payload):
    event = {
        "record_type": "event",
        "seq": 0,
        "measurement_profile_id": "synthetic-profile",
        "mono_start_ms": 60000,
        "mono_end_ms": 70000,
        "active_duration_ms": 5000,
        "max_1s_rms_dbfs": -20.0,
        "baseline_dbfs": None,
        "threshold_profile_id": "relative-sound-60s-10db-6db",
        "merged": True,
        "incomplete": False,
        "quality": ["merged"],
    }
    payload["records"] = [event]
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200
        event["seq"] = 1
        event["threshold_profile_id"] = "wrong-profile"
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 422
        event["active_duration_ms"] = 11000
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 422


def test_rate_buckets_are_bounded_and_excess_receives_retry_after(settings, payload):
    gate = DeviceGate(settings)
    for i in range(200):
        gate._allowed(str(i))
    assert len(gate._buckets) == 128
    with browser(settings) as web:
        results = [web.post(INGEST_PATH, json=payload, headers=headers()) for _ in range(30)]
    limited = [response for response in results if response.status_code == 429]
    assert limited and limited[-1].headers["retry-after"] == "1"


def test_environment_backup_preserves_all_tables_and_detects_payload_corruption(settings, payload):
    storage.ingest(settings.data_dir, "synthetic-device", EnvironmentBatch.model_validate(payload))
    before = counts(settings)
    saved = create_backup(settings)
    assert all(saved["counts"][key] == value for key, value in before.items())
    restored = check_restore(settings, Path(saved["backup_path"]))
    assert restored["counts"] == saved["counts"]
    path = settings.data_dir / saved["backup_path"]
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE environment_records SET seq=99")
    with pytest.raises(BackupError, match="backup_environment_payload_invalid"):
        check_restore(settings, path)
    assert counts(settings) == before


def test_explicit_migration_leaves_garmin_tables_unchanged(tmp_path):
    directory = tmp_path / "data"
    with Repository(directory) as repository:
        repository.start_run({"synthetic": True})
    with sqlite3.connect(directory / "sleepscape.sqlite3") as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ["runs", "raw_snapshots", "normalized_records", "requests"]
        }
    storage.initialize(directory)
    storage.initialize(directory)
    with sqlite3.connect(directory / "sleepscape.sqlite3") as connection:
        after = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in before}
    assert before == after


def test_registration_and_rotation_write_private_token_file_not_stdout(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    token_file = tmp_path / ".secrets" / "device.json"
    args = [
        "sleepscape",
        "environment-register",
        "--device-id",
        "synthetic-device",
        "--profile-id",
        "synthetic-profile",
        "--profile-file",
        str(FIXTURES / "environment-profile.json"),
        "--token-file",
        str(token_file),
    ]
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", args)
    cli.main()
    output = capsys.readouterr().out
    token = json.loads(token_file.read_bytes())["token"]
    assert token not in output
    assert json.loads(output)["status"] == "registered"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert token_file.parent.stat().st_mode & 0o777 == 0o700
    assert storage.authenticate(settings.data_dir, token) == "synthetic-device"
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        digest = connection.execute("SELECT token_hash FROM environment_devices").fetchone()[0]
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert token not in digest
    rotated = tmp_path / ".secrets" / "rotated.json"
    rotate_device_token(settings, "synthetic-device", rotated)
    new = json.loads(rotated.read_bytes())["token"]
    assert storage.authenticate(settings.data_dir, token) is None
    assert storage.authenticate(settings.data_dir, new) == "synthetic-device"
    with pytest.raises(storage.EnvironmentError, match="device_token_path_not_new"):
        rotate_device_token(settings, "synthetic-device", rotated)


def test_profile_is_immutable_and_registration_never_overwrites_token(settings, tmp_path, profile):
    path = tmp_path / ".private" / "duplicate.json"
    with pytest.raises(storage.EnvironmentError, match="device_already_registered"):
        register_device(
            settings,
            "synthetic-device",
            "synthetic-profile",
            FIXTURES / "environment-profile.json",
            path,
        )
    assert not path.exists()
    register_profile(
        settings, "synthetic-device", "synthetic-profile", FIXTURES / "environment-profile.json"
    )
    changed = profile.model_copy(update={"placement_id": "new-placement"})
    with pytest.raises(storage.EnvironmentError, match="profile_conflict"):
        storage.add_profile(settings.data_dir, "synthetic-device", "synthetic-profile", changed)
    storage.add_profile(settings.data_dir, "synthetic-device", "different-profile", changed)
    assert counts(settings)["environment_profiles"] == 2


def test_oversized_content_length_is_bounded_before_integer_conversion(settings):
    with browser(settings) as web:
        response = web.post(
            INGEST_PATH,
            content=b"{}",
            headers={**headers(), "Content-Length": "9" * 5000},
        )
    assert response.status_code == 413
    assert counts(settings)["environment_records"] == 0


def test_literal_version_and_profile_parameters_do_not_accept_boolean(payload, profile):
    payload["schema_version"] = True
    with pytest.raises(ValidationError):
        EnvironmentBatch.model_validate(payload)
    values = profile.model_dump(mode="json")
    values["audio_channels"] = True
    with pytest.raises(ValidationError):
        MeasurementProfile.model_validate(values)


def test_quarantine_count_is_preserved_and_cannot_be_negative(settings, payload):
    payload["device_status"]["quarantined_count"] = 3
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200
        payload["device_status"]["quarantined_count"] = -1
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 422
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        value = connection.execute("SELECT payload_json FROM environment_device_status").fetchone()[
            0
        ]
    assert json.loads(value)["quarantined_count"] == 3


def test_current_veml_profile_requires_complete_finite_consistent_metadata(profile):
    values = profile.model_dump(mode="json")
    assert values["light_sensor"] == "dfrobot-sen0228-veml7700"
    for name in ("light_measurement", "i2c_bus"):
        incomplete = copy.deepcopy(values)
        incomplete.pop(name)
        with pytest.raises(ValidationError):
            MeasurementProfile.model_validate(incomplete)
    for section, name, value in (
        ("light_measurement", "gain", True),
        ("light_measurement", "resolution_lux", float("nan")),
        ("light_measurement", "resolution_lux", float("inf")),
        ("light_measurement", "range_max_lux", 550.494),
        ("light_measurement", "auto_range", 0),
        ("light_measurement", "auto_range", True),
        ("light_measurement", "low_light_limit_lux", 600),
        ("i2c_bus", "light_address", 0x23),
        ("i2c_bus", "temperature_address", 0x10),
        ("i2c_bus", "scl_gpio", 21),
        ("i2c_bus", "supply_voltage_v", float("inf")),
    ):
        changed = copy.deepcopy(values)
        changed[section][name] = value
        with pytest.raises(ValidationError):
            MeasurementProfile.model_validate(changed)


def test_sensor_profile_change_requires_new_id_and_preserves_old_hash(settings, profile, payload):
    values = profile.model_dump(mode="json")
    values.pop("light_measurement")
    values.pop("i2c_bus")
    values.update(light_sensor="m5stack-u136-bh1750", firmware_version="0.1.0")
    historical = MeasurementProfile.model_validate(values)
    storage.add_profile(settings.data_dir, "synthetic-device", "historical-profile", historical)
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        original = connection.execute(
            "SELECT payload_json,content_hash FROM environment_profiles WHERE profile_id=?",
            ("historical-profile",),
        ).fetchone()
    assert "light_measurement" not in json.loads(original[0])
    assert "i2c_bus" not in json.loads(original[0])
    # Re-reading and re-registering an existing immutable profile must not add null fields.
    loaded = MeasurementProfile.model_validate_json(original[0])
    storage.add_profile(settings.data_dir, "synthetic-device", "historical-profile", loaded)
    with pytest.raises(storage.EnvironmentError, match="profile_conflict"):
        storage.add_profile(settings.data_dir, "synthetic-device", "historical-profile", profile)
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        preserved = connection.execute(
            "SELECT payload_json,content_hash FROM environment_profiles WHERE profile_id=?",
            ("historical-profile",),
        ).fetchone()
    assert preserved == original
    # A historical low-light flag uses its historical measurement convention.
    payload["records"][0]["measurement_profile_id"] = "historical-profile"
    light = payload["records"][0]["metrics"]["illuminance_lux"]
    light.update(mean=0.5, min=0.5, max=0.5, quality=["low_light_limit"])
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200
    snapshot = create_backup(settings)
    check_restore(settings, Path(snapshot["backup_path"]))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_illuminance_never_becomes_zero(payload, value):
    payload["records"][0]["metrics"]["illuminance_lux"].update(mean=value, min=value, max=value)
    with pytest.raises(ValidationError):
        EnvironmentBatch.model_validate(payload)


def test_veml_profile_bounds_and_low_light_flags_are_checked_before_any_commit(settings, payload):
    light = payload["records"][0]["metrics"]["illuminance_lux"]
    with browser(settings) as web:
        light.update(mean=550.494, min=550.494, max=550.494, quality=[])
        rejected = web.post(INGEST_PATH, json=payload, headers=headers())
        assert rejected.status_code == 422
        assert rejected.json()["error"] == "illuminance_outside_measurement_profile"
        assert counts(settings)["environment_records"] == 0
        assert counts(settings)["environment_time_anchors"] == 0
        light.update(mean=0.5, min=0.5, max=0.5, quality=["low_light_limit"])
        rejected = web.post(INGEST_PATH, json=payload, headers=headers())
        assert rejected.status_code == 422
        assert rejected.json()["error"] == "light_quality_profile_mismatch"
        light["quality"] = []
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200


def test_veml_last_valid_count_and_low_light_threshold_remain_valid(settings, payload):
    with browser(settings) as web:
        for seq, level, flags in (
            (0, 550.485595703125, []),  # Actual float32 round-trip at raw 65534.
            (1, 0.1008, []),
            (2, 0.0924, ["low_light_limit"]),
            (3, 0.0, ["low_light_limit"]),
        ):
            changed = copy.deepcopy(payload)
            changed["records"][0]["seq"] = seq
            changed["records"][0]["metrics"]["illuminance_lux"].update(
                mean=level, min=level, max=level, quality=flags
            )
            assert web.post(INGEST_PATH, json=changed, headers=headers()).status_code == 200


def test_saturated_illuminance_remains_invalid_null_not_darkness(settings, payload):
    light = payload["records"][0]["metrics"]["illuminance_lux"]
    light.update(
        state="invalid", valid_count=0, mean=None, min=None, max=None, quality=["out_of_range"]
    )
    with browser(settings) as web:
        assert web.post(INGEST_PATH, json=payload, headers=headers()).status_code == 200
    stored = json.loads(record_rows(settings)[0][-1])["metrics"]["illuminance_lux"]
    assert stored["mean"] is None
    assert stored["valid_count"] == 0 and stored["observed_count"] == 60
    assert stored["state"] == "invalid" and stored["quality"] == ["out_of_range"]
    light.update(mean=0.0, min=0.0, max=0.0)
    with pytest.raises(ValidationError):
        EnvironmentBatch.model_validate(payload)


def test_known_firmware_profile_rejects_self_consistent_but_wrong_driver_parameters(profile):
    values = profile.model_dump(mode="json")
    values["light_measurement"].update(
        gain=1.0, resolution_lux=0.0168, range_max_lux=1100.9712, als_conf=0x0080
    )
    with pytest.raises(ValidationError, match="known_firmware_sensor_configuration_mismatch"):
        MeasurementProfile.model_validate(values)
    # An intentionally new firmware version can describe a different fixed-gain configuration.
    values["firmware_version"] = "future-synthetic"
    changed = MeasurementProfile.model_validate(values)
    assert changed.light_measurement.gain == 1.0


@pytest.mark.parametrize(
    ("section", "name", "value"),
    [
        ("light_measurement", "integration_ms", 800),
        ("light_measurement", "als_conf", 0),
        ("light_measurement", "power_saving_enabled", True),
        ("light_measurement", "low_light_limit_lux", 1.0),
        ("light_measurement", "reference", "different-conversion-reference"),
        ("i2c_bus", "frequency_hz", 400000),
        ("i2c_bus", "supply_voltage_v", 5.0),
        ("i2c_bus", "sda_gpio", 26),
        ("i2c_bus", "hub_model", "other-hub"),
    ],
)
def test_known_firmware_registration_cannot_claim_different_sensor_settings(
    profile, section, name, value
):
    values = profile.model_dump(mode="json")
    values[section][name] = value
    with pytest.raises(ValidationError, match="known_firmware_sensor_configuration_mismatch"):
        MeasurementProfile.model_validate(values)
