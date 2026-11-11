"""Native production C -> strict HTTPS ASGI endpoint -> exact C ACK interoperability.

Requires the pinned SDK cJSON source (make firmware-setup). Firmware CI sets
SLEEPSCAPE_REQUIRE_FIRMWARE_WIRE=1 so absence is an error, not a skipped acceptance.
"""

import copy
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.storage import environment as storage
from sleepscape.web.device_security import INGEST_PATH

ROOT = Path(__file__).parents[1]
TOKEN = "synthetic_" + "x" * 43
SCENARIOS = [
    "full",
    "ready",
    "event",
    "missing",
    "partial",
    "zero",
    "overflow",
    "anchor",
    "status",
    "maximal",
    "aggregate",
    "light-low",
    "light-saturated",
    "light-bus-error",
    "light-warming",
    "light-recovered",
    "light-legacy",
]


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    cjson = ROOT / ".firmware-tools/esp-idf/components/json/cJSON"
    if compiler is None or not (cjson / "cJSON.c").is_file():
        if os.environ.get("SLEEPSCAPE_REQUIRE_FIRMWARE_WIRE") == "1":
            pytest.fail("Firmware acceptance requires a C compiler and make firmware-setup")
        pytest.skip("Native wire acceptance needs a C compiler and make firmware-setup")
    output = tmp_path_factory.mktemp("native-wire") / "wire"
    dependency = output.with_suffix(".o")
    built = subprocess.run(
        [
            compiler,
            "-std=c11",
            "-Wno-deprecated-declarations",
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            "-g",
            "-c",
            str(cjson / "cJSON.c"),
            "-o",
            str(dependency),
        ],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    built = subprocess.run(
        [
            compiler,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            "-g",
            f"-I{ROOT / 'firmware/components/sc_core/include'}",
            f"-I{ROOT / 'firmware/main'}",
            f"-I{cjson}",
            str(ROOT / "firmware/components/sc_core/sc_core.c"),
            str(ROOT / "firmware/components/sc_core/sc_record.c"),
            str(ROOT / "firmware/components/sc_core/sc_aggregate.c"),
            str(ROOT / "firmware/components/sc_core/sc_veml7700.c"),
            str(ROOT / "firmware/main/sc_wire.c"),
            str(ROOT / "firmware/tests/wire_fixture.c"),
            str(dependency),
            "-lm",
            "-o",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    return output


def run_native(native, command, scenario="full", payload=None):
    args = [str(native), command]
    if command not in {"guards", "profile"}:
        args.append(scenario)
    return subprocess.run(args, input=payload, capture_output=True, text=True)


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("SLEEPSCAPE_", "GARMIN_PERSONAL_API_")):
            monkeypatch.delenv(name)
    profile = MeasurementProfile.model_validate_json(
        (ROOT / "tests/fixtures/environment-profile.json").read_bytes()
    )
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", environment_ingest_enabled=True)
    storage.register(settings.data_dir, "synthetic-device", "synthetic-profile", profile, TOKEN)
    historical = profile.model_dump(mode="json")
    historical.pop("light_measurement")
    historical.pop("i2c_bus")
    historical.update(light_sensor="m5stack-u136-bh1750", firmware_version="0.1.0")
    storage.add_profile(
        settings.data_dir,
        "synthetic-device",
        "historical-profile",
        MeasurementProfile.model_validate(historical),
    )
    with TestClient(create_app(settings), base_url="https://localhost") as client:
        yield client, settings


def send(client, payload):
    return client.post(
        INGEST_PATH,
        content=payload,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_native_binary_roundtrip_database_and_idempotent_ack(native, web, scenario):
    client, settings = web
    result = run_native(native, "batch", scenario)
    assert result.returncode == 0, result.stderr
    batch = EnvironmentBatch.model_validate_json(result.stdout)
    assert len(result.stdout.encode()) <= 65_536
    response = send(client, result.stdout)
    assert response.status_code == 200, response.text
    checked = run_native(native, "ack", scenario, response.text)
    assert checked.returncode == 0, checked.stderr
    duplicate = send(client, result.stdout)
    assert duplicate.status_code == 200
    assert run_native(native, "ack", scenario, duplicate.text).returncode == 0
    counts = storage.status(settings.data_dir)["counts"]
    assert counts["environment_records"] == len(batch.records)
    assert counts["environment_time_anchors"] == len(batch.time_anchors)
    assert counts["environment_device_status"] == int(batch.device_status is not None)
    with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as connection:
        rows = connection.execute("SELECT payload_json FROM environment_records").fetchall()
    stored = [json.loads(row[0]) for row in rows]
    if scenario == "zero":
        sound = stored[0]["metrics"]["sound_relative"]
        assert sound["energy_mean_dbfs"] is None and sound["valid_count"] == 60
        assert "below_numeric_floor" in sound["quality"]
    if scenario == "missing":
        for channel in stored[0]["metrics"].values():
            assert channel["state"] == "missing" and channel["valid_count"] == 0
    if scenario == "full":
        sound = stored[0]["metrics"]["sound_relative"]
        assert sound["detected_count"] is None and sound["event_valid_duration_ms"] == 0
        assert stored[0]["metrics"]["illuminance_lux"]["mean"] == 0
    if scenario == "ready":
        assert stored[0]["metrics"]["sound_relative"]["detected_count"] == 0
    if scenario == "aggregate":
        events = [row for row in stored if row["record_type"] == "event"]
        assert len(events) == 2
        assert events[0]["continues_next"]
        assert events[1]["continued_from_previous"]
    if scenario.startswith("light-"):
        light = stored[0]["metrics"]["illuminance_lux"]
        if scenario in {"light-saturated", "light-bus-error", "light-warming"}:
            assert light["valid_count"] == 0 and light["mean"] is None
            required = {
                "light-saturated": "out_of_range",
                "light-bus-error": "bus_error",
                "light-warming": "warming",
            }[scenario]
            assert required in light["quality"]
            if scenario == "light-warming":
                assert "sensor_error" not in light["quality"]
        elif scenario == "light-low":
            assert light["valid_count"] == 60
            assert light["mean"] == pytest.approx(0.0924)
            assert "low_light_limit" in light["quality"]
        elif scenario == "light-recovered":
            assert light["valid_count"] == 58 and light["observed_count"] == 60
            assert {"bus_error", "sensor_error", "warming", "partial_window"} <= set(
                light["quality"]
            )
            assert light["mean"] == pytest.approx(0.1008)
        elif scenario == "light-legacy":
            assert stored[0]["measurement_profile_id"] == "historical-profile"
            assert light["mean"] == pytest.approx(0.05)
            assert "low_light_limit" not in light["quality"]
    if scenario == "anchor":
        assert batch.time_anchors[0].utc_at == "2026-01-01T00:00:00.123Z"
    if scenario == "status":
        assert batch.device_status.ranges_truncated
        assert batch.device_status.lost_ranges == []
        assert batch.device_status.quarantined_count == 4


def test_native_wire_rejects_bad_source_record_and_count(native):
    result = run_native(native, "guards")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "device",
        "boot",
        "version",
        "missing",
        "seq",
        "type",
        "status",
        "anchor",
        "status_seq",
        "extra",
        "duplicate_record",
        "extra_field",
        "null_status",
        "bool_version",
        "duplicate_key",
        "trailing",
        "deep",
        "oversize",
    ],
)
def test_native_requires_complete_exact_ack(native, web, mutation):
    client, _ = web
    payload = run_native(native, "batch", "event").stdout
    response = send(client, payload)
    assert response.status_code == 200
    ack = copy.deepcopy(response.json())
    if mutation == "device":
        ack["device_id"] = "other-device"
    elif mutation == "boot":
        ack["boot_id"] = "other-boot"
    elif mutation == "version":
        ack["schema_version"] = 2
    elif mutation == "missing":
        ack["records"].pop()
    elif mutation == "seq":
        ack["records"][0]["seq"] += 1
    elif mutation == "type":
        ack["records"][0]["record_type"] = "unknown"
    elif mutation == "status":
        ack["records"][0]["status"] = "rejected"
    elif mutation == "anchor":
        ack["time_anchors"][0]["anchor_id"] = "1"
    elif mutation == "status_seq":
        ack["device_status"]["status_seq"] += 1
    elif mutation == "extra":
        ack["records"].append(copy.deepcopy(ack["records"][0]))
    elif mutation == "duplicate_record":
        ack["records"][1] = copy.deepcopy(ack["records"][0])
    elif mutation == "extra_field":
        ack["records"][0]["unexpected"] = 1
    elif mutation == "null_status":
        ack["device_status"] = None
    elif mutation == "bool_version":
        ack["schema_version"] = True
    encoded = json.dumps(ack)
    if mutation == "duplicate_key":
        encoded = encoded.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    elif mutation == "trailing":
        encoded += "unexpected"
    elif mutation == "deep":
        encoded = "[" * 40 + encoded + "]" * 40
    elif mutation == "oversize":
        encoded += " " * 65536
    checked = run_native(native, "ack", "event", encoded)
    assert checked.returncode == 1, checked.stderr


def test_registered_profile_examples_match_compiled_sensor_configuration(native):
    """Changing driver constants must invalidate stale operator registration examples."""
    result = run_native(native, "profile")
    assert result.returncode == 0, result.stderr
    compiled = json.loads(result.stdout)
    required = {"firmware_version", "light_sensor", "light_measurement", "i2c_bus"}
    assert required <= compiled.keys()
    for path in (
        ROOT / "firmware/profile.example.json",
        ROOT / "tests/fixtures/environment-profile.json",
    ):
        registered = MeasurementProfile.model_validate_json(path.read_bytes())
        actual = registered.model_dump(mode="json")
        for name, expected in compiled.items():
            if isinstance(expected, dict):
                assert actual[name].keys() == expected.keys()
                for parameter, value in expected.items():
                    found = actual[name][parameter]
                    if isinstance(value, float):
                        assert found == pytest.approx(value, rel=1e-12, abs=1e-12)
                    else:
                        assert found == value, f"{path.name}: {name}.{parameter} drift"
            else:
                assert actual[name] == expected, f"{path.name}: firmware {name} configuration drift"
