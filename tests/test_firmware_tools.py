"""Offline regressions for build gates and credential-safe USB provisioning."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sleepscape_firmware_tools", Path(__file__).parents[1] / "tools/firmware.py"
)
assert SPEC is not None and SPEC.loader is not None
fw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fw)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def config():
    return {
        "device_id": "bedroom-01",
        "profile_id": "bedroom-v1",
        "server_url": "https://sleep.example.com/api/v1/ingest/environment-batches",
        "token": "test-device-token-" + "x" * 32,
        "ssid": "Home Wi-Fi",
        "password": "test-wifi-password",
    }


@pytest.fixture
def build_files(workspace):
    directory = workspace / "firmware/build"
    files = {
        "0x1000": "bootloader/bootloader.bin",
        "0x8000": "partition_table/partition-table.bin",
        "0x10000": "sleepscape_environment.bin",
    }
    for name in files.values():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"firmware-test")
    (directory / "flasher_args.json").write_text(json.dumps({"flash_files": files}))
    return directory, files


def write_private(directory, name, contents):
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = directory / name
    path.write_text(contents)
    path.chmod(0o600)
    return path


def test_setup_stops_at_wrong_sdk_commit_before_install(workspace, monkeypatch):
    sdk = workspace / ".firmware-tools/esp-idf"
    (sdk / ".git").mkdir(parents=True)
    monkeypatch.setattr(fw, "python_path", lambda: workspace / ".venv/bin/python")
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return "wrong-commit\n"

    monkeypatch.setattr(fw, "command", run)
    with pytest.raises(fw.FirmwareError, match="sdk_commit_mismatch"):
        fw.setup()
    assert len(commands) == 1
    assert commands[0][-2:] == ["rev-parse", "HEAD"]


@pytest.mark.parametrize(
    ("dirty", "modules", "error"),
    [
        (" M tools/idf.py", "", "sdk_checkout_modified"),
        ("", "-abcdef components/a", "sdk_submodules_not_pinned"),
        ("", "+abcdef components/a", "sdk_submodules_not_pinned"),
    ],
)
def test_sdk_verification_rejects_modified_or_missing_sources(
    workspace, monkeypatch, dirty, modules, error
):
    (workspace / ".firmware-tools/esp-idf/.git").mkdir(parents=True)
    responses = iter([fw.SDK_COMMIT, dirty, modules])
    monkeypatch.setattr(fw, "command", lambda *args, **kwargs: next(responses))
    with pytest.raises(fw.FirmwareError, match=error):
        fw.verify_sdk()


def test_build_checks_real_binary_after_build_and_size(workspace, monkeypatch, build_files):
    calls = []
    monkeypatch.setattr(fw, "idf_environment", lambda: {"TEST": "1"})
    monkeypatch.setattr(fw, "run_idf", lambda args, env: calls.append(args))
    image = build_files[0] / "sleepscape_environment.bin"
    with image.open("wb") as stream:
        stream.truncate(fw.MAX_APP_BYTES + 1)
    with pytest.raises(fw.FirmwareError, match="firmware_exceeds_1_8_mib_budget"):
        fw.build()
    assert calls == [["build"], ["size"]]


def test_flash_only_writes_explicit_safe_ranges(build_files, monkeypatch):
    directory, files = build_files
    # Build metadata cannot inject options or a full-chip erase into flashing.
    (directory / "flasher_args.json").write_text(
        json.dumps({"flash_files": files, "extra_esptool_args": {"erase_all": True}})
    )
    calls = []
    env = {"SDK": "test"}
    monkeypatch.setattr(fw, "idf_environment", lambda: env)
    monkeypatch.setattr(fw, "idf_python", lambda value: Path("/sdk/python"))
    monkeypatch.setattr(fw, "command", lambda args, **kwargs: calls.append((args, kwargs)))
    fw.flash("/dev/cu.usbserial-example")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:5] == ["/sdk/python", "-m", "esptool", "--chip", "esp32"]
    assert args[args.index("--port") + 1] == "/dev/cu.usbserial-example"
    assert args[args.index("--baud") + 1] == "115200"
    assert args[-6::2] == ["0x1000", "0x8000", "0x10000"]
    assert not any("erase" in arg for arg in args)
    assert kwargs == {"env": env}


@pytest.fixture
def first_install(build_files, monkeypatch):
    class TerminalStreams:
        stdin = SimpleNamespace(isatty=lambda: True)
        stdout = SimpleNamespace(isatty=lambda: True)

        @property
        def stderr(self):
            return sys.stderr

    calls = []
    env = {"SDK": "test"}
    monkeypatch.setattr(fw, "idf_environment", lambda: env)
    monkeypatch.setattr(fw, "idf_python", lambda value: Path("/sdk/python"))
    monkeypatch.setattr(fw, "command", lambda args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(fw, "sys", TerminalStreams())
    return calls, env


def test_first_install_requires_confirmation_before_erase_then_safe_write(
    first_install, monkeypatch, capsys
):
    calls, env = first_install

    def confirm(prompt):
        assert calls == []
        assert "FIRST-INSTALL" in prompt
        warning = capsys.readouterr().out
        assert "/dev/cu.usbserial-example" in warning
        assert "ALL factory firmware, configuration, and data" in warning
        return "FIRST-INSTALL"

    monkeypatch.setattr("builtins.input", confirm)
    assert fw.main(["first-flash", "--port", "/dev/cu.usbserial-example"]) == 0
    assert len(calls) == 2
    erase, write = [args for args, _ in calls]
    for args, kwargs in calls:
        assert args[:5] == ["/sdk/python", "-m", "esptool", "--chip", "esp32"]
        assert args[args.index("--port") + 1] == "/dev/cu.usbserial-example"
        assert kwargs == {"env": env}
    assert erase[-1] == "erase_flash"
    assert "write_flash" in write
    assert write[-6::2] == ["0x1000", "0x8000", "0x10000"]
    assert not any("erase" in arg for arg in write)
    output = capsys.readouterr().out
    assert "previous device data was erased" in output
    assert "were not erased" not in output


@pytest.mark.parametrize("answer", ["", "yes", "first-install", "FIRST-INSTALL "])
def test_first_install_cancel_never_accesses_device(first_install, monkeypatch, answer):
    calls, _ = first_install
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    with pytest.raises(fw.FirmwareError, match="first_install_cancelled"):
        fw.first_flash("/dev/cu.usbserial-example")
    assert calls == []


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_first_install_interrupted_confirmation_never_accesses_device(
    first_install, monkeypatch, interruption, capsys
):
    calls, _ = first_install

    def interrupt(prompt):
        raise interruption

    monkeypatch.setattr("builtins.input", interrupt)
    assert fw.main(["first-flash", "--port", "/dev/cu.usbserial-example"]) == 1
    assert calls == []
    assert "first_install_cancelled" in capsys.readouterr().err


@pytest.mark.parametrize("stream", ["stdin", "stdout"])
def test_first_install_noninteractive_never_prompts_or_accesses_device(
    first_install, monkeypatch, stream
):
    calls, _ = first_install
    monkeypatch.setattr(getattr(fw.sys, stream), "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Must not prompt"))
    with pytest.raises(fw.FirmwareError, match="first_install_requires_interactive_terminal"):
        fw.first_flash("/dev/cu.usbserial-example")
    assert calls == []


@pytest.mark.parametrize(
    ("invalid", "error"),
    [
        ("missing_manifest", "flash_manifest_missing_or_invalid"),
        ("invalid_manifest", "flash_manifest_missing_or_invalid"),
        ("private_range", "flash_manifest_must_preserve_nvs_and_queue"),
        ("missing_image", "flash_manifest_image_missing"),
        ("empty_bootloader", "flash_manifest_image_empty"),
        ("empty_partition", "flash_manifest_image_empty"),
        ("empty_application", "flash_manifest_image_empty"),
        ("oversize_bootloader", "flash_image_overlaps_private_partition"),
        ("oversize_partition", "flash_image_overlaps_private_partition"),
        ("oversize_application", "firmware_exceeds_1_8_mib_budget"),
    ],
)
def test_first_install_validates_build_before_confirmation_or_erase(
    first_install, build_files, monkeypatch, invalid, error
):
    calls, _ = first_install
    directory, files = build_files
    manifest = directory / "flasher_args.json"
    if invalid == "missing_manifest":
        manifest.unlink()
    elif invalid == "invalid_manifest":
        manifest.write_text("not json")
    elif invalid == "private_range":
        files["0x210000"] = files.pop("0x10000")
        manifest.write_text(json.dumps({"flash_files": files}))
    elif invalid == "missing_image":
        (directory / files["0x10000"]).unlink()
    else:
        image, limit = {
            "bootloader": (files["0x1000"], 0x7000),
            "partition": (files["0x8000"], 0x1000),
            "application": (files["0x10000"], fw.MAX_APP_BYTES),
        }[invalid.split("_", 1)[1]]
        with (directory / image).open("wb") as stream:
            stream.truncate(0 if invalid.startswith("empty_") else limit + 1)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Must not prompt"))
    with pytest.raises(fw.FirmwareError, match=error):
        fw.first_flash("/dev/cu.usbserial-example")
    assert calls == []


@pytest.mark.parametrize("dependency", ["idf_environment", "idf_python"])
def test_first_install_requires_usable_sdk_before_confirmation_or_erase(
    first_install, monkeypatch, dependency
):
    calls, _ = first_install

    def unavailable(*args):
        raise fw.FirmwareError("sdk_unavailable")

    monkeypatch.setattr(fw, dependency, unavailable)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Must not prompt"))
    with pytest.raises(fw.FirmwareError, match="sdk_unavailable"):
        fw.first_flash("/dev/cu.usbserial-example")
    assert calls == []


def test_first_install_erase_failure_stops_before_write(first_install, monkeypatch, capsys):
    calls, _ = first_install
    monkeypatch.setattr("builtins.input", lambda prompt: "FIRST-INSTALL")

    def erase_failure(args, **kwargs):
        calls.append((args, kwargs))
        raise fw.FirmwareError("command_failed")

    monkeypatch.setattr(fw, "command", erase_failure)
    assert fw.main(["first-flash", "--port", "/dev/cu.usbserial-example"]) == 1
    assert len(calls) == 1
    assert calls[0][0][-1] == "erase_flash"
    output = capsys.readouterr()
    assert "command_failed" in output.err
    assert "First installation complete" not in output.out


def test_first_install_write_failure_discloses_erase_and_normal_flash_retry(
    first_install, monkeypatch, capsys
):
    calls, _ = first_install
    monkeypatch.setattr("builtins.input", lambda prompt: "FIRST-INSTALL")

    def write_failure(args, **kwargs):
        calls.append((args, kwargs))
        if "write_flash" in args:
            raise fw.FirmwareError("command_failed")

    monkeypatch.setattr(fw, "command", write_failure)
    assert fw.main(["first-flash", "--port", "/dev/cu.usbserial-example"]) == 1
    assert len(calls) == 2
    output = capsys.readouterr()
    assert "Previous data is already erased" in output.err
    assert "retry ordinary flash with the same --port" in output.err
    assert "First installation complete" not in output.out


@pytest.mark.parametrize(
    "args",
    [
        ["first-flash"],
        ["first-flash", "--port", "/dev/cu.test", "--yes"],
        ["first-flash", "--po", "/dev/cu.test"],
    ],
)
def test_first_install_has_no_automatic_port_or_confirmation_bypass(args):
    with pytest.raises(SystemExit):
        fw.parser().parse_args(args)


@pytest.mark.parametrize("offset", ["0x9000", "0x210000", "0x0"])
def test_flash_rejects_private_partition_or_merged_image(build_files, offset):
    directory, files = build_files
    files[offset] = files.pop("0x10000")
    (directory / "flasher_args.json").write_text(json.dumps({"flash_files": files}))
    with pytest.raises(fw.FirmwareError, match="preserve_nvs_and_queue"):
        fw.validate_flash_manifest()


@pytest.mark.parametrize(
    ("image", "size"),
    [("bootloader/bootloader.bin", 0x7001), ("partition_table/partition-table.bin", 0x1001)],
)
def test_flash_rejects_sector_overlap_with_private_data(build_files, image, size):
    directory, _ = build_files
    with (directory / image).open("wb") as stream:
        stream.truncate(size)
    with pytest.raises(fw.FirmwareError, match="flash_image_overlaps_private_partition"):
        fw.validate_flash_manifest()


def test_flash_rejects_unexpected_binary_path(build_files):
    directory, files = build_files
    files["0x10000"] = "../other.bin"
    (directory / "flasher_args.json").write_text(json.dumps({"flash_files": files}))
    with pytest.raises(fw.FirmwareError, match="flash_manifest_unexpected_image"):
        fw.validate_flash_manifest()


@pytest.mark.parametrize("port", ["", "auto", "--erase-all", "/dev/cu.test\n"])
@pytest.mark.parametrize("action", ["flash", "first_flash", "status", "serial_status"])
def test_no_automatic_or_ambiguous_flash_port(port, action, monkeypatch):
    monkeypatch.setattr(fw, "idf_environment", lambda: pytest.fail("Must reject before SDK/USB"))
    with pytest.raises(fw.FirmwareError):
        getattr(fw, action)(port)


def test_configuration_preserves_utf8_wifi_and_uses_expected_uart_schema(config):
    config["ssid"] = "我的卧室 Wi-Fi"
    payload = fw.validate_configuration(config)
    assert payload == {"command": "configure", **config}
    assert len(json.dumps(payload, ensure_ascii=False).encode()) < 2048


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("server_url", "http://sleep.example.com" + fw.ENDPOINT),
        ("server_url", "https://user:secret@sleep.example.com" + fw.ENDPOINT),
        ("server_url", "https://sleep.example.com" + fw.ENDPOINT + "?token=secret"),
        ("server_url", "https://sleep.example.com/"),
        ("server_url", "https://sleep.example.com:0" + fw.ENDPOINT),
        ("token", "GCDC_" + "a" * 40),
        ("token", "short-token"),
        ("token", "x" * 42),
        ("ssid", "中" * 11),
        ("ssid", "home\nnetwork"),
        ("password", "short"),
        ("device_id", "a" * 65),
        ("profile_id", ""),
    ],
)
def test_configuration_rejects_unusable_or_unsafe_values(config, field, value):
    config[field] = value
    with pytest.raises(fw.FirmwareError):
        fw.validate_configuration(config)


def test_private_file_requires_protected_parent_and_file(workspace):
    path = write_private(workspace / "private", "device.json", "{}")
    assert fw.private_text(path) == "{}"
    path.chmod(0o644)
    with pytest.raises(fw.FirmwareError, match="mode_600"):
        fw.private_text(path)
    path.chmod(0o600)
    path.parent.chmod(0o755)
    with pytest.raises(fw.FirmwareError, match="owner_700"):
        fw.private_text(path)


def test_private_file_rejects_symlink_and_oversize(workspace):
    target = write_private(workspace / "private", "token", "a" * (fw.MAX_PRIVATE_BYTES + 1))
    with pytest.raises(fw.FirmwareError, match="owner_or_size_invalid"):
        fw.private_text(target)
    link = target.with_name("link")
    link.symlink_to(target)
    with pytest.raises(fw.FirmwareError, match="private_file_unreadable"):
        fw.private_text(link)


def test_config_file_and_separate_token_file(workspace, config):
    token = config.pop("token")
    config_file = write_private(workspace / "private", "device.json", json.dumps(config))
    token_file = write_private(
        workspace / "private",
        "device.json.token",
        json.dumps(
            {"device_id": config["device_id"], "profile_id": config["profile_id"], "token": token}
        ),
    )
    assert fw.configuration(config_file, token_file)["token"] == token
    config["token"] = token
    config_file.write_text(json.dumps(config))
    with pytest.raises(fw.FirmwareError, match="token_must_have_one_source"):
        fw.configuration(config_file, token_file)


def test_configure_sends_secrets_only_on_stdin(config, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(fw, "configuration", lambda *_: fw.validate_configuration(config))
    monkeypatch.setattr(fw, "idf_environment", lambda: {"SDK": "test"})
    monkeypatch.setattr(fw, "idf_python", lambda _: Path("/sdk/python"))
    monkeypatch.setattr(fw, "command", lambda args, **kwargs: calls.append((args, kwargs)))
    fw.configure("/dev/cu.usbserial-example", None, None)
    args, kwargs = calls[0]
    assert kwargs["capture"] is True
    assert json.loads(kwargs["input_text"]) == {"command": "configure", **config}
    for secret in (config["token"], config["password"]):
        assert secret not in repr(args)
        assert secret not in repr(kwargs["env"])
        assert secret not in capsys.readouterr().out


class FakeSerial:
    lines = [b'{"status":"configured"}\n']
    instance = None

    def __init__(self, **kwargs):
        assert kwargs == {"port": None, "baudrate": 115200, "timeout": 0.2, "write_timeout": 3}
        self.dtr = self.rts = True
        self.port = None
        self.opened = False
        self.closed = False
        self.reset = False
        self.written = b""
        self.responses = iter(self.lines)
        FakeSerial.instance = self

    def __enter__(self):
        assert self.port == "/dev/cu.usbserial-example"
        assert self.dtr is False and self.rts is False
        self.opened = True
        return self

    def __exit__(self, *_):
        self.closed = True

    def reset_input_buffer(self):
        self.reset = True

    def write(self, value):
        assert self.opened and self.reset
        self.written = value
        return len(value)

    def flush(self):
        pass

    def readline(self, size):
        assert size == 2049
        return next(self.responses, b"")


@pytest.mark.parametrize("configured", [True, False])
def test_status_wrapper_uses_sdk_and_only_prints_safe_fields(monkeypatch, capsys, configured):
    calls = []
    env = {"SDK": "test"}
    monkeypatch.setattr(fw, "idf_environment", lambda: env)
    monkeypatch.setattr(fw, "idf_python", lambda _: Path("/sdk/python"))
    response = {
        "status": "ready",
        "configured": configured,
        "firmware_version": "0.2.0",
        "ssid": "private-wifi",
        "token": "private-token",
        "password": "private-password",
    }

    def worker(args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(response)

    monkeypatch.setattr(fw, "command", worker)
    assert fw.main(["status", "--port", "/dev/cu.usbserial-example"]) == 0
    assert calls == [
        (
            [
                "/sdk/python",
                str(Path(fw.__file__).resolve()),
                "_serial-status",
                "--port",
                "/dev/cu.usbserial-example",
            ],
            {"env": env, "capture": True},
        )
    ]
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "status": "ready",
        "configured": configured,
        "firmware_version": "0.2.0",
    }
    assert "private-" not in output.out + output.err + repr(calls)


def test_status_usb_ignores_noise_and_filters_extra_private_fields(monkeypatch, capsys):
    monkeypatch.setattr(fw.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        FakeSerial,
        "lines",
        [
            b"boot: private-token\n",
            b"{bad-json}\n",
            b'{"status":"configured"}\n',
            b'{"status":"ready","configured":"true","firmware_version":"0.2.0"}\n',
            b'{"status":"ready","configured":true,"firmware_version":"private-token"}\n',
            b'{"status":"ready","configured":false,"firmware_version":"0.2.0",'
            b'"ssid":"private-wifi","token":"private-token"}\n',
        ],
    )
    assert fw.serial_status("/dev/cu.usbserial-example", serial_class=FakeSerial) == {
        "status": "ready",
        "configured": False,
        "firmware_version": "0.2.0",
    }
    handle = FakeSerial.instance
    assert handle.closed
    assert handle.written == b'{"command":"status"}\n'
    assert handle.dtr is False and handle.rts is False
    assert capsys.readouterr() == ("", "")


def test_status_missing_version_is_explicitly_unknown(monkeypatch):
    monkeypatch.setattr(fw.time, "sleep", lambda _: None)
    monkeypatch.setattr(FakeSerial, "lines", [b'{"status":"ready","configured":true}\n'])
    result = fw.serial_status("/dev/cu.usbserial-example", serial_class=FakeSerial)
    assert result == {"status": "ready", "configured": True, "firmware_version": "unknown"}


@pytest.mark.parametrize("lines", [[], [b"{bad-json}\n"], [b"private-token\n"]])
def test_status_missing_valid_response_times_out_without_echoing(monkeypatch, capsys, lines):
    monkeypatch.setattr(fw.time, "sleep", lambda _: None)
    monkeypatch.setattr(FakeSerial, "lines", lines)
    clock = iter([0, 0.1, 21])
    monkeypatch.setattr(fw.time, "monotonic", lambda: next(clock))
    with pytest.raises(fw.FirmwareError, match="status_response_timeout"):
        fw.serial_status("/dev/cu.usbserial-example", serial_class=FakeSerial)
    assert FakeSerial.instance.closed
    assert capsys.readouterr() == ("", "")


def test_status_rejects_unbounded_serial_noise(monkeypatch, capsys):
    monkeypatch.setattr(fw.time, "sleep", lambda _: None)
    monkeypatch.setattr(FakeSerial, "lines", [b"x" * 2049] * 33)
    with pytest.raises(fw.FirmwareError, match="status_response_limit"):
        fw.serial_status("/dev/cu.usbserial-example", serial_class=FakeSerial)
    assert FakeSerial.instance.closed
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "response",
    [
        "private-token",
        "null",
        '{"status":"ready","configured":1,"token":"private-token"}',
        '{"status":"ready","configured":true,"firmware_version":"private-token"}',
        '{"status":"ready","configured":true,"firmware_version":2}',
    ],
)
def test_status_wrapper_rejects_invalid_worker_output_without_echoing(
    monkeypatch, capsys, response
):
    monkeypatch.setattr(fw, "idf_environment", lambda: {})
    monkeypatch.setattr(fw, "idf_python", lambda _: Path("/sdk/python"))
    monkeypatch.setattr(fw, "command", lambda *args, **kwargs: response)
    assert fw.main(["status", "--port", "/dev/cu.usbserial-example"]) == 1
    assert capsys.readouterr() == ("", "Firmware tool: device_status_invalid\n")


def test_status_worker_entry_prints_only_sanitized_result(monkeypatch, capsys):
    safe = {"status": "ready", "configured": False, "firmware_version": "unknown"}
    calls = []

    def inspect(port):
        calls.append(port)
        return safe

    monkeypatch.setattr(fw, "serial_status", inspect)
    assert fw.main(["_serial-status", "--port", "/dev/cu.usbserial-example"]) == 0
    assert calls == ["/dev/cu.usbserial-example"]
    assert json.loads(capsys.readouterr().out) == safe


def test_usb_ignores_noise_without_echoing_and_requires_saved_ack(config, monkeypatch, capsys):
    monkeypatch.setattr(fw.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        FakeSerial,
        "lines",
        [config["token"].encode(), b"{bad-json}\n", b'{"status":"configured"}\n'],
    )
    fw.serial_configure("/dev/cu.usbserial-example", config, serial_class=FakeSerial)
    handle = FakeSerial.instance
    assert handle.closed
    assert handle.written.endswith(b"\n")
    assert json.loads(handle.written) == {"command": "configure", **config}
    assert capsys.readouterr() == ("", "")


def test_usb_rejected_configuration_is_not_success(config, monkeypatch):
    monkeypatch.setattr(fw.time, "sleep", lambda _: None)
    monkeypatch.setattr(FakeSerial, "lines", [b'{"status":"error","code":"config_invalid"}\n'])
    with pytest.raises(fw.FirmwareError, match="device_rejected_configuration"):
        fw.serial_configure("/dev/cu.usbserial-example", config, serial_class=FakeSerial)
    assert FakeSerial.instance.closed


def test_usb_missing_ack_times_out_instead_of_claiming_success(config, monkeypatch):
    monkeypatch.setattr(fw.time, "sleep", lambda _: None)
    monkeypatch.setattr(FakeSerial, "lines", [])
    clock = iter([0, 0.1, 21])
    monkeypatch.setattr(fw.time, "monotonic", lambda: next(clock))
    with pytest.raises(fw.FirmwareError, match="configuration_ack_timeout"):
        fw.serial_configure("/dev/cu.usbserial-example", config, serial_class=FakeSerial)
    assert FakeSerial.instance.closed


def test_subprocess_failure_never_surfaces_captured_credentials(monkeypatch, capsys):
    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "private-token", "private-wifi-password")

    monkeypatch.setattr(fw.subprocess, "run", failed)
    monkeypatch.setattr(fw, "setup", lambda: fw.command(["fake"], capture=True))
    assert fw.main(["setup"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Firmware tool: command_failed\n"


def test_check_requires_native_wire_interoperability_without_usb(workspace, monkeypatch):
    runner = workspace / "firmware/tests/run_host_tests.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\nexit 0\n")
    calls = []
    python = workspace / ".venv/bin/python"
    monkeypatch.setattr(fw, "python_path", lambda: python)
    monkeypatch.setattr(fw, "command", lambda args, **kwargs: calls.append((args, kwargs)))
    fw.check()
    assert calls[0] == (["bash", str(runner)], {})
    assert calls[1][0] == [str(python), "-m", "pytest", "tests/test_firmware_wire.py"]
    assert calls[1][1]["env"]["SLEEPSCAPE_REQUIRE_FIRMWARE_WIRE"] == "1"


def test_public_help_has_no_secret_argument_or_internal_worker():
    help_text = fw.parser().format_help()
    assert "_serial-configure" not in help_text
    assert "_serial-status" not in help_text
    with pytest.raises(SystemExit):
        fw.parser().parse_args(["flash"])
    with pytest.raises(SystemExit):
        fw.parser().parse_args(["configure", "--port", "/dev/cu.test", "--token", "secret"])


def test_install_environment_uses_base_python_not_nested_virtualenv(workspace, monkeypatch):
    python = workspace / "uv-python/bin/python3.12"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(fw, "python_path", lambda: workspace / ".venv/bin/python")
    monkeypatch.setattr(fw, "command", lambda *args, **kwargs: str(python))
    monkeypatch.setenv("VIRTUAL_ENV", "/other/venv")
    env = fw.base_environment()
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"].split(fw.os.pathsep)[0] == str(python.parent)
    assert env["IDF_PYTHON_ENV_PATH"].endswith("python_env/idf5.5_py3.12_env")


def test_no_terminal_never_falls_back_to_echoed_secret_input(monkeypatch):
    def no_terminal(_):
        fw.warnings.warn("Cannot control terminal echo", fw.getpass.GetPassWarning, stacklevel=2)

    monkeypatch.setattr(fw.getpass, "getpass", no_terminal)
    with pytest.raises(fw.FirmwareError, match="private_terminal_required_use_config_file"):
        fw.hidden_input("secret")


def test_setup_installs_pinned_cmake_and_ninja_locally(workspace, monkeypatch):
    sdk = workspace / ".firmware-tools/esp-idf"
    sdk.mkdir(parents=True)
    python = workspace / ".venv/bin/python"
    calls = []
    monkeypatch.setattr(fw, "python_path", lambda: python)
    monkeypatch.setattr(fw, "verify_sdk", lambda: None)
    monkeypatch.setattr(fw, "base_environment", lambda: {"IDF_TOOLS_PATH": "local-tools"})
    monkeypatch.setattr(fw, "idf_environment", lambda: {})
    monkeypatch.setattr(fw, "command", lambda args, **kwargs: calls.append((args, kwargs)))
    fw.setup()
    assert calls[0][0] == ["bash", str(sdk / "install.sh"), "esp32"]
    assert calls[1][0] == [
        str(python),
        str(sdk / "tools/idf_tools.py"),
        "install",
        "cmake@3.30.2",
        "ninja@1.12.1",
    ]
    assert all(kwargs["env"] == {"IDF_TOOLS_PATH": "local-tools"} for _, kwargs in calls)


def test_registration_json_supplies_identity_without_reentering_secrets(
    workspace, config, monkeypatch
):
    package = {name: config[name] for name in ("device_id", "profile_id", "token")}
    path = write_private(workspace / "private", "registered.json", json.dumps(package))
    replies = iter([config["server_url"], config["ssid"]])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    monkeypatch.setattr(fw, "hidden_input", lambda _: config["password"])
    assert fw.configuration(None, path) == {"command": "configure", **config}


def test_rotated_json_prompts_only_missing_profile(workspace, config, monkeypatch):
    package = {name: config[name] for name in ("device_id", "token")}
    path = write_private(workspace / "private", "rotated.json", json.dumps(package))
    config_path = write_private(
        workspace / "private",
        "wifi.json",
        json.dumps({name: config[name] for name in ("server_url", "ssid", "password")}),
    )
    prompts = []

    def reply(prompt):
        prompts.append(prompt)
        return config["profile_id"]

    monkeypatch.setattr("builtins.input", reply)
    assert fw.configuration(config_path, path) == {"command": "configure", **config}
    assert prompts == ["Measurement profile ID: "]


@pytest.mark.parametrize("field", ["device_id", "profile_id"])
def test_token_file_identity_conflict_fails_before_usb(workspace, config, field):
    package = {name: config[name] for name in ("device_id", "profile_id", "token")}
    token_path = write_private(workspace / "private", "registered.json", json.dumps(package))
    config.pop("token")
    config[field] = "another-device-or-profile"
    config_path = write_private(workspace / "private", "wifi.json", json.dumps(config))
    with pytest.raises(fw.FirmwareError, match="identity_conflict"):
        fw.configuration(config_path, token_path)


def test_token_file_accepts_only_unambiguous_registration_json(workspace, config):
    path = write_private(workspace / "private", "token.json", config["token"])
    with pytest.raises(fw.FirmwareError, match="private_configuration_json_invalid"):
        fw.token_credentials(path)
    path.write_text('{"device_id":"one","device_id":"two","token":"private"}')
    with pytest.raises(fw.FirmwareError, match="private_configuration_json_invalid"):
        fw.token_credentials(path)


def test_real_registration_and_rotation_files_can_provision_offline(workspace, config):
    from sleepscape.config import Settings
    from sleepscape.services.environment import register_device, rotate_device_token

    settings = Settings(_env_file=None, data_dir=workspace / "data")
    profile = Path(__file__).parents[1] / "firmware/profile.example.json"
    token_path = workspace / "private/device.json"
    register_device(settings, config["device_id"], config["profile_id"], profile, token_path)
    package = fw.token_credentials(token_path)
    assert package["device_id"] == config["device_id"]
    assert package["profile_id"] == config["profile_id"]
    assert len(package["token"]) == 43
    config.pop("token")
    config_path = write_private(workspace / "private", "wifi.json", json.dumps(config))
    assert fw.configuration(config_path, token_path)["token"] == package["token"]
    rotated_path = workspace / "private/rotated.json"
    rotate_device_token(settings, config["device_id"], rotated_path)
    rotated = fw.configuration(config_path, rotated_path)
    assert rotated["token"] != package["token"]
    assert rotated["profile_id"] == config["profile_id"]
