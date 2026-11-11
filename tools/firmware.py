#!/usr/bin/env python3
"""Build and provision SleepScape firmware without exposing device credentials."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import stat
import subprocess
import sys
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SDK_TAG = "v5.5.6"
SDK_COMMIT = "880a203ae603d08a94d06aa01e0d6761958e5e9e"
SDK_REPOSITORY = "https://github.com/espressif/esp-idf.git"
BUILD_TOOLS = ("cmake@3.30.2", "ninja@1.12.1")
MAX_APP_BYTES = int(1.8 * 1024 * 1024)
MAX_PRIVATE_BYTES = 8192
ENDPOINT = "/api/v1/ingest/environment-batches"
APP_NAME = "sleepscape_environment"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")


class FirmwareError(Exception):
    """A stable, credential-free failure suitable for terminal output."""


def paths() -> tuple[Path, Path, Path]:
    return ROOT / ".firmware-tools/esp-idf", ROOT / ".firmware-tools/tools", ROOT / "firmware"


def command(args: list[str], *, env=None, capture=False, input_text=None) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            env=env,
            text=True,
            input=input_text,
            capture_output=capture,
            check=False,
        )
    except OSError as exc:
        raise FirmwareError("required_program_unavailable") from exc
    if result.returncode != 0:
        raise FirmwareError("command_failed")
    return result.stdout if capture else ""


def python_path() -> Path:
    python = ROOT / ".venv/bin/python"
    if not python.is_file():
        raise FirmwareError("project_python_missing_run_uv_sync")
    version = command(
        [str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"],
        capture=True,
    ).strip()
    if version != "3.12":
        raise FirmwareError("project_python_requires_3_12")
    return python


def verify_sdk() -> None:
    sdk, _, _ = paths()
    if not (sdk / ".git").exists():
        raise FirmwareError("sdk_missing_run_setup")
    commit = command(["git", "-C", str(sdk), "rev-parse", "HEAD"], capture=True).strip()
    if commit != SDK_COMMIT:
        raise FirmwareError("sdk_commit_mismatch")
    # A pinned HEAD alone does not attest to modified source or missing submodules.
    dirty = command(["git", "-C", str(sdk), "status", "--porcelain"], capture=True)
    if dirty.strip():
        raise FirmwareError("sdk_checkout_modified")
    modules = command(["git", "-C", str(sdk), "submodule", "status", "--recursive"], capture=True)
    if any(line.startswith(("-", "+", "U")) for line in modules.splitlines()):
        raise FirmwareError("sdk_submodules_not_pinned")


def base_environment() -> dict[str, str]:
    sdk, tools, _ = paths()
    python = python_path()
    # ESP-IDF cannot create its own environment from inside another virtualenv.
    # Reuse the project's uv-managed base interpreter; never install into it.
    base_python = Path(
        command(
            [str(python), "-c", "import sys; print(sys._base_executable)"], capture=True
        ).strip()
    )
    if not base_python.is_file():
        raise FirmwareError("project_base_python_missing")
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env.update(
        IDF_PATH=str(sdk),
        IDF_TOOLS_PATH=str(tools),
        PATH=str(base_python.parent) + os.pathsep + env.get("PATH", ""),
        IDF_PYTHON_ENV_PATH=str(tools / "python_env/idf5.5_py3.12_env"),
    )
    return env


def idf_environment() -> dict[str, str]:
    verify_sdk()
    sdk, _, _ = paths()
    python = python_path()
    # Paths are positional arguments, never interpolated shell code. Keep export
    # output private; its environment can contain inherited local credentials.
    shell = (
        'set -e; . "$1/export.sh" >/dev/null; '
        '"$2" -c "import json,os; print(json.dumps(dict(os.environ)))"'
    )
    raw = command(
        ["bash", "-c", shell, "sleepscape-idf-env", str(sdk), str(python)],
        env=base_environment(),
        capture=True,
    )
    try:
        environment = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise FirmwareError("sdk_environment_invalid") from exc
    if not isinstance(environment, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in environment.items()
    ):
        raise FirmwareError("sdk_environment_invalid")
    return environment


def idf_python(env: dict[str, str]) -> Path:
    result = Path(env.get("IDF_PYTHON_ENV_PATH", "")) / "bin/python"
    if not result.is_file():
        raise FirmwareError("sdk_python_missing_run_setup")
    return result


def run_idf(args: list[str], env: dict[str, str]) -> None:
    sdk, _, firmware = paths()
    command([str(idf_python(env)), str(sdk / "tools/idf.py"), "-C", str(firmware), *args], env=env)


def setup() -> None:
    sdk, tools, _ = paths()
    python = python_path()
    if not sdk.exists():
        sdk.parent.mkdir(parents=True, exist_ok=True)
        command(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                SDK_TAG,
                "--recurse-submodules",
                "--shallow-submodules",
                "--jobs",
                "8",
                SDK_REPOSITORY,
                str(sdk),
            ]
        )
    verify_sdk()
    tools.mkdir(parents=True, exist_ok=True)
    # install.sh downloads only Espressif's pinned ESP32 tools under this project.
    command(["bash", str(sdk / "install.sh"), "esp32"], env=base_environment())
    # On macOS/Linux these are on-request in the pinned SDK tools.json, so
    # install.sh esp32 alone is insufficient on a computer without build tools.
    # Espressif verifies its bundled SHA-256 checksums and exports local paths.
    command(
        [str(python), str(sdk / "tools/idf_tools.py"), "install", *BUILD_TOOLS],
        env=base_environment(),
    )
    idf_environment()
    print(f"SDK ready: {SDK_TAG}, Python {python.name}; tools stay in .firmware-tools/")


def app_size() -> tuple[int, Path]:
    _, _, firmware = paths()
    image = firmware / "build" / f"{APP_NAME}.bin"
    if not image.is_file():
        raise FirmwareError("firmware_binary_missing_run_build")
    size = image.stat().st_size
    if size <= 0 or size > MAX_APP_BYTES:
        raise FirmwareError("firmware_exceeds_1_8_mib_budget")
    return size, image


def build() -> None:
    env = idf_environment()
    run_idf(["build"], env)
    run_idf(["size"], env)
    size, image = app_size()
    print(f"Firmware size: {size} bytes / {MAX_APP_BYTES} byte gate ({image.name})")


def check() -> None:
    _, _, firmware = paths()
    runner = firmware / "tests/run_host_tests.sh"
    if not runner.is_file():
        raise FirmwareError("host_test_runner_missing")
    command(["bash", str(runner)])
    env = dict(os.environ)
    env["SLEEPSCAPE_REQUIRE_FIRMWARE_WIRE"] = "1"
    command([str(python_path()), "-m", "pytest", "tests/test_firmware_wire.py"], env=env)


def validate_flash_manifest() -> dict[int, Path]:
    _, _, firmware = paths()
    directory = firmware / "build"
    try:
        manifest = json.loads((directory / "flasher_args.json").read_text())
    except (OSError, ValueError) as exc:
        raise FirmwareError("flash_manifest_missing_or_invalid") from exc
    if not isinstance(manifest, dict):
        raise FirmwareError("flash_manifest_missing_or_invalid")
    files = manifest.get("flash_files")
    if not isinstance(files, dict) or len(files) != 3:
        raise FirmwareError("flash_manifest_must_preserve_nvs_and_queue")
    expected = {
        0x1000: Path("bootloader/bootloader.bin"),
        0x8000: Path("partition_table/partition-table.bin"),
        0x10000: Path(f"{APP_NAME}.bin"),
    }
    actual = {}
    for offset, name in files.items():
        try:
            address = int(offset, 0)
            file = Path(name)
        except (TypeError, ValueError) as exc:
            raise FirmwareError("flash_manifest_invalid_entry") from exc
        if address in actual or address not in expected:
            raise FirmwareError("flash_manifest_must_preserve_nvs_and_queue")
        location = file if file.is_absolute() else directory / file
        if location.resolve() != (directory / expected[address]).resolve():
            raise FirmwareError("flash_manifest_unexpected_image")
        if not location.is_file():
            raise FirmwareError("flash_manifest_image_missing")
        actual[address] = location
    if set(actual) != set(expected):
        raise FirmwareError("flash_manifest_must_preserve_nvs_and_queue")
    if any(path.stat().st_size <= 0 for path in actual.values()):
        raise FirmwareError("flash_manifest_image_empty")
    # The first two ranges must not erase or write neighboring NVS partitions.
    if actual[0x1000].stat().st_size > 0x7000 or actual[0x8000].stat().st_size > 0x1000:
        raise FirmwareError("flash_image_overlaps_private_partition")
    app_size()
    return actual


def validate_port(value: str) -> str:
    if not value or any(ord(c) < 32 for c in value) or value.startswith("-"):
        raise FirmwareError("explicit_serial_port_required")
    if not (
        value.startswith("/dev/cu.")
        or value.startswith("/dev/tty")
        or re.fullmatch(r"COM[1-9][0-9]*", value)
    ):
        raise FirmwareError("serial_port_name_invalid")
    return value


def flash_arguments(port: str, env: dict[str, str], images: dict[int, Path]) -> list[str]:
    # Only these validated byte ranges are written. Ignore extra flasher flags
    # from build metadata, and never rebuild automatically while flashing.
    return [
        str(idf_python(env)),
        "-m",
        "esptool",
        "--chip",
        "esp32",
        "--port",
        port,
        "--baud",
        "115200",
        "--before",
        "default_reset",
        "--after",
        "hard_reset",
        "write_flash",
        "--flash_mode",
        "dio",
        "--flash_freq",
        "40m",
        "--flash_size",
        "4MB",
        *[part for offset, path in sorted(images.items()) for part in (hex(offset), str(path))],
    ]


def flash(port: str) -> None:
    validate_port(port)
    env = idf_environment()
    images = validate_flash_manifest()
    command(flash_arguments(port, env, images), env=env)
    print("Firmware written; NVS configuration and offline queue were not erased.")


def first_flash(port: str) -> None:
    validate_port(port)
    env = idf_environment()
    images = validate_flash_manifest()
    write_args = flash_arguments(port, env, images)
    # Check everything offline before asking permission or opening the port.
    # A pipe, redirected input, or an environment flag cannot authorize erasure.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise FirmwareError("first_install_requires_interactive_terminal")
    print(f"FIRST INSTALLATION ONLY — selected ESP32 device: {port}")
    print("This will erase ALL factory firmware, configuration, and data on this device.")
    print("Use ordinary flash for later upgrades to preserve configuration and queued data.")
    try:
        confirmation = input("Type FIRST-INSTALL exactly to erase and install: ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise FirmwareError("first_install_cancelled") from exc
    if confirmation != "FIRST-INSTALL":
        raise FirmwareError("first_install_cancelled")
    command(
        [
            write_args[0],
            "-m",
            "esptool",
            "--chip",
            "esp32",
            "--port",
            port,
            "--baud",
            "115200",
            "--before",
            "default_reset",
            "--after",
            "hard_reset",
            "erase_flash",
        ],
        env=env,
    )
    print("Device erase completed; all previous firmware, configuration, and data is now erased.")
    try:
        command(write_args, env=env)
    except (FirmwareError, KeyboardInterrupt):
        print(
            "Firmware write did not complete after erasure. Previous data is already erased; "
            "retry ordinary flash with the same --port, without another full-chip erase.",
            file=sys.stderr,
        )
        raise
    print(
        "First installation complete; all previous device data was erased. Configure over USB next."
    )


def private_text(path: Path) -> str:
    try:
        if path.parent.is_symlink():
            raise FirmwareError("private_directory_symlink_not_allowed")
        parent = path.parent.stat()
        if stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != os.getuid():
            raise FirmwareError("private_directory_requires_owner_700")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise FirmwareError("private_file_requires_mode_600")
            if info.st_uid != os.getuid() or info.st_size > MAX_PRIVATE_BYTES:
                raise FirmwareError("private_file_owner_or_size_invalid")
            raw = stream.read(MAX_PRIVATE_BYTES + 1)
        if len(raw) > MAX_PRIVATE_BYTES:
            raise FirmwareError("private_file_too_large")
        return raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise FirmwareError("private_file_unreadable") from exc


def validate_configuration(config: dict) -> dict[str, str]:
    fields = {"device_id", "profile_id", "server_url", "token", "ssid", "password"}
    if not isinstance(config, dict) or set(config) not in (fields, fields | {"command"}):
        raise FirmwareError("configuration_fields_invalid")
    if config.get("command", "configure") != "configure":
        raise FirmwareError("configuration_command_invalid")
    if not all(isinstance(config[name], str) for name in fields):
        raise FirmwareError("configuration_values_must_be_text")
    if any(any(ord(c) < 32 or ord(c) == 127 for c in config[name]) for name in fields):
        raise FirmwareError("configuration_control_characters_forbidden")
    if not all(IDENTIFIER.fullmatch(config[name]) for name in ("device_id", "profile_id")):
        raise FirmwareError("device_or_profile_id_invalid")
    if not TOKEN.fullmatch(config["token"]) or config["token"].startswith("GCDC_"):
        raise FirmwareError("device_token_invalid")
    if not 1 <= len(config["ssid"].encode()) <= 32:
        raise FirmwareError("wifi_ssid_requires_1_to_32_bytes")
    password = config["password"]
    if password and not 8 <= len(password.encode()) <= 63:
        raise FirmwareError("wifi_password_length_invalid")
    try:
        url = urlsplit(config["server_url"])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path != ENDPOINT
            or url.query
            or url.fragment
            or len(config["server_url"].encode()) > 255
            or " " in config["server_url"]
        ):
            raise FirmwareError("server_requires_exact_https_endpoint")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise FirmwareError("server_port_invalid")
    except ValueError as exc:
        raise FirmwareError("server_url_invalid") from exc
    return {"command": "configure", **{key: config[key] for key in sorted(fields)}}


def hidden_input(prompt: str) -> str:
    # getpass otherwise falls back to an echoed stdin read when no terminal exists.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning as exc:
        raise FirmwareError("private_terminal_required_use_config_file") from exc


def private_json(path: Path) -> dict:
    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate field")
            value[key] = item
        return value

    try:
        config = json.loads(private_text(path), object_pairs_hook=unique_fields)
    except (ValueError, RecursionError) as exc:
        raise FirmwareError("private_configuration_json_invalid") from exc
    if not isinstance(config, dict):
        raise FirmwareError("private_configuration_json_invalid")
    return config


def token_credentials(path: Path) -> dict[str, str]:
    credentials = private_json(path)
    fields = {"device_id", "token"}
    if set(credentials) not in (fields, fields | {"profile_id"}) or not all(
        isinstance(value, str) for value in credentials.values()
    ):
        raise FirmwareError("device_token_file_fields_invalid")
    if not TOKEN.fullmatch(credentials["token"]) or credentials["token"].startswith("GCDC_"):
        raise FirmwareError("device_token_invalid")
    if not IDENTIFIER.fullmatch(credentials["device_id"]) or (
        "profile_id" in credentials and not IDENTIFIER.fullmatch(credentials["profile_id"])
    ):
        raise FirmwareError("device_or_profile_id_invalid")
    return credentials


def configuration(config_file: Path | None, token_file: Path | None) -> dict[str, str]:
    credentials = token_credentials(token_file) if token_file else {}
    if config_file is not None:
        config = private_json(config_file)
        if credentials:
            if "token" in config:
                raise FirmwareError("token_must_have_one_source")
            for name in ("device_id", "profile_id"):
                if name in credentials:
                    if name in config and config[name] != credentials[name]:
                        raise FirmwareError("token_file_configuration_identity_conflict")
                    config[name] = credentials[name]
            config["token"] = credentials["token"]
            if "profile_id" not in config:
                config["profile_id"] = input("Measurement profile ID: ").strip()
    else:
        config = {
            "device_id": credentials.get("device_id") or input("Device ID: ").strip(),
            "profile_id": credentials.get("profile_id")
            or input("Measurement profile ID: ").strip(),
            "server_url": input(f"Server HTTPS URL ending in {ENDPOINT}: ").strip(),
            "ssid": input("2.4 GHz Wi-Fi name: "),
            "password": hidden_input("Wi-Fi password (hidden; empty for open Wi-Fi): "),
            "token": credentials.get("token") or hidden_input("Device token (hidden): "),
        }
    return validate_configuration(config)


def configure(port: str, config_file: Path | None, token_file: Path | None) -> None:
    validate_port(port)
    payload = configuration(config_file, token_file)
    env = idf_environment()
    # Use SDK's existing pyserial. Secrets go only through the pipe, not argv/env.
    command(
        [str(idf_python(env)), str(Path(__file__).resolve()), "_serial-configure", "--port", port],
        env=env,
        capture=True,
        input_text=json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n",
    )
    print("Device confirmed configuration saved; it will restart and reconnect.")


def serial_configure(
    port: str, payload: dict[str, str], *, serial_class=None, timeout=20.0
) -> None:
    validate_port(port)
    payload = validate_configuration(payload)
    if serial_class is None:
        try:
            from serial import Serial
        except ImportError as exc:
            raise FirmwareError("sdk_pyserial_missing_run_setup") from exc
        serial_class = Serial
    wire = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    if len(wire) > 2048:
        raise FirmwareError("configuration_wire_size_invalid")
    try:
        # Set control lines before opening so a USB serial adapter doesn't leave
        # the ESP32 held in reset. A board-specific reset remains a real-device test.
        port_handle = serial_class(port=None, baudrate=115200, timeout=0.2, write_timeout=3)
        port_handle.dtr = False
        port_handle.rts = False
        port_handle.port = port
        # pyserial's context manager opens a closed handle on entry, so assign
        # the port and control lines before entering it.
        with port_handle:
            time.sleep(2)
            port_handle.reset_input_buffer()
            if port_handle.write(wire) != len(wire):
                raise FirmwareError("configuration_usb_write_incomplete")
            port_handle.flush()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = port_handle.readline(2049)
                if not line or len(line) > 2048:
                    continue
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(response, dict) and response.get("status") == "configured":
                    return
                if isinstance(response, dict) and response.get("status") == "error":
                    raise FirmwareError("device_rejected_configuration")
            raise FirmwareError("configuration_ack_timeout")
    except FirmwareError:
        raise
    except Exception as exc:
        raise FirmwareError("configuration_usb_failed") from exc


def status_fields(response: object) -> dict[str, str | bool]:
    if (
        not isinstance(response, dict)
        or response.get("status") != "ready"
        or type(response.get("configured")) is not bool
    ):
        raise FirmwareError("device_status_invalid")
    version = response.get("firmware_version", "unknown")
    if not isinstance(version, str) or (
        version != "unknown"
        and re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", version) is None
    ):
        raise FirmwareError("device_status_invalid")
    return {"status": "ready", "configured": response["configured"], "firmware_version": version}


def status(port: str) -> None:
    validate_port(port)
    env = idf_environment()
    raw = command(
        [str(idf_python(env)), str(Path(__file__).resolve()), "_serial-status", "--port", port],
        env=env,
        capture=True,
    )
    try:
        response = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise FirmwareError("device_status_invalid") from exc
    # Even worker output is revalidated and filtered, never echoed directly.
    print(json.dumps(status_fields(response), separators=(",", ":")))


def serial_status(port: str, *, serial_class=None, timeout=20.0) -> dict[str, str | bool]:
    validate_port(port)
    if serial_class is None:
        try:
            from serial import Serial
        except ImportError as exc:
            raise FirmwareError("sdk_pyserial_missing_run_setup") from exc
        serial_class = Serial
    wire = b'{"command":"status"}\n'
    try:
        port_handle = serial_class(port=None, baudrate=115200, timeout=0.2, write_timeout=3)
        port_handle.dtr = False
        port_handle.rts = False
        port_handle.port = port
        with port_handle:
            time.sleep(2)
            port_handle.reset_input_buffer()
            if port_handle.write(wire) != len(wire):
                raise FirmwareError("status_usb_write_incomplete")
            port_handle.flush()
            deadline = time.monotonic() + timeout
            received = 0
            while time.monotonic() < deadline:
                line = port_handle.readline(2049)
                received += len(line)
                if received > 65536:
                    raise FirmwareError("status_response_limit")
                if not line or len(line) > 2048 or not line.endswith(b"\n"):
                    continue
                try:
                    return status_fields(json.loads(line))
                except (FirmwareError, ValueError, UnicodeError, RecursionError):
                    continue
            raise FirmwareError("status_response_timeout")
    except FirmwareError:
        raise
    except Exception as exc:
        raise FirmwareError("status_usb_failed") from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="SleepScape firmware build and USB provisioning", allow_abbrev=False
    )
    sub = result.add_subparsers(dest="action", required=True)
    sub.add_parser("setup", help="Install pinned ESP-IDF/ESP32 tools inside .firmware-tools")
    sub.add_parser("build", help="Compile firmware and check the 1.8 MiB application budget")
    sub.add_parser(
        "check", help="Run host C and required firmware/server wire tests; no device or network"
    )
    flashing = sub.add_parser(
        "flash", help="Write firmware to the explicitly selected port; preserve NVS/queue"
    )
    flashing.add_argument("--port", required=True, help="Example: /dev/cu.usbserial-XXXX")
    first_install = sub.add_parser(
        "first-flash",
        help="First installation only: interactively confirm full-chip erase, then write firmware",
        allow_abbrev=False,
    )
    first_install.add_argument("--port", required=True, help="Example: /dev/cu.usbserial-XXXX")
    inspection = sub.add_parser(
        "status",
        help="Read only firmware version and configuration state over USB",
        allow_abbrev=False,
    )
    inspection.add_argument("--port", required=True)
    provisioning = sub.add_parser(
        "configure",
        help="Send Wi-Fi/server configuration over USB without echoing secrets",
        allow_abbrev=False,
    )
    provisioning.add_argument("--port", required=True)
    provisioning.add_argument(
        "--config-file", type=Path, help="Private JSON file: mode 600, parent directory 700"
    )
    provisioning.add_argument(
        "--token-file",
        type=Path,
        help="Private JSON from environment-register/rotate-token; never a Garmin key",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] in {"_serial-configure", "_serial-status"}:
        internal = argparse.ArgumentParser()
        internal.add_argument("--port", required=True)
        args = internal.parse_args(values[1:])
        args.action = values[0]
    else:
        args = parser().parse_args(values)
    try:
        if args.action == "setup":
            setup()
        elif args.action == "build":
            build()
        elif args.action == "check":
            check()
        elif args.action == "flash":
            flash(args.port)
        elif args.action == "first-flash":
            first_flash(args.port)
        elif args.action == "status":
            status(args.port)
        elif args.action == "_serial-status":
            print(json.dumps(serial_status(args.port), separators=(",", ":")))
        elif args.action == "configure":
            configure(args.port, args.config_file, args.token_file)
        elif args.action == "_serial-configure":
            raw = sys.stdin.buffer.read(2049)
            if len(raw) > 2048:
                raise FirmwareError("configuration_wire_size_invalid")
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise FirmwareError("configuration_input_invalid") from exc
            serial_configure(args.port, payload)
    except (FirmwareError, EOFError, KeyboardInterrupt) as exc:
        message = str(exc) if isinstance(exc, FirmwareError) else "operation_cancelled"
        print(f"Firmware tool: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
