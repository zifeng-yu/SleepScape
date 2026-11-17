"""Explicit local USB capture using the existing environment contract and transactions."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from sleepscape.config import Settings
from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.services import backup
from sleepscape.services.environment import identifiers, parse_batch, read_profile
from sleepscape.storage import environment as storage

USB_FIRMWARE_VERSION = "0.3.0"


class USBCollectionError(Exception):
    """Stable messages only; never print unknown serial payloads or database content."""


def prepare(
    settings: Settings, device_id: str, profile_id: str, profile_path: Path, placement_id: str
) -> MeasurementProfile:
    identifiers(device_id, profile_id)
    identifiers(placement_id)
    original = read_profile(profile_path)
    profile = MeasurementProfile.model_validate(
        {**original.model_dump(mode="json"), "placement_id": placement_id}
    )
    if profile.firmware_version != USB_FIRMWARE_VERSION:
        raise USBCollectionError("usb_measurement_profile_version_mismatch")
    if storage.local_registration_required(settings.data_dir, device_id, profile_id, profile):
        if (settings.data_dir / "sleepscape.sqlite3").is_file():
            saved = backup.create_backup(settings)
            restored = backup.check_restore(settings, Path(saved["backup_path"]))
            if saved["status"] != "verified" or restored["status"] != "verified":
                raise USBCollectionError("usb_registration_backup_unverified")
        storage.register_local(settings.data_dir, device_id, profile_id, profile)
    return profile


def _clock_response(response: dict, device_id: str) -> tuple[str, int]:
    boot_id = response.get("boot_id")
    mono = response.get("monotonic_ms")
    try:
        identifiers(device_id, boot_id)
    except (storage.EnvironmentError, TypeError):
        raise USBCollectionError("usb_clock_response_invalid") from None
    if not isinstance(boot_id, str) or type(mono) is not int or not 0 <= mono <= 2**53 - 1:
        raise USBCollectionError("usb_clock_response_invalid")
    return boot_id, mono


def _anchor(settings: Settings, device_id: str, response: dict, now: datetime) -> None:
    boot_id, mono = _clock_response(response, device_id)
    # This is an explicitly labeled host-clock observation, not an NTP sync and
    # never a replacement for a sensor sample's own monotonic timestamp.
    item = EnvironmentBatch.model_validate(
        {
            "schema_version": 1,
            "device_id": device_id,
            "boot_id": boot_id,
            "records": [],
            "time_anchors": [
                {
                    "anchor_id": "host-usb-" + uuid4().hex,
                    "monotonic_ms": mono,
                    "utc_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "source": "host_usb",
                    "uncertainty_ms": None,
                    "quality": [],
                }
            ],
        }
    )
    storage.ingest(settings.data_dir, device_id, item)


def capture(
    settings: Settings,
    device_id: str,
    profile_id: str,
    duration: int,
    exchange: Callable[[dict], dict],
    *,
    progress: Callable[[dict], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict:
    """Receive durably, then ACK. A failed or interrupted exchange is safe to replay."""
    if type(duration) is not int or not 1 <= duration <= 86400:
        raise USBCollectionError("usb_duration_requires_1_to_86400_seconds")
    identifiers(device_id, profile_id)
    summary = {
        "status": "completed",
        "transport": "usb",
        "device_id": device_id,
        "profile_id": profile_id,
        "accepted_records": 0,
        "duplicate_records": 0,
        "received_batches": 0,
        "device_stopped": False,
        "tail_partial_window_saved": False,
    }
    started = False
    start_attempted = False
    failure: BaseException | None = None
    try:
        start_attempted = True
        response = exchange(
            {"command": "usb_start", "device_id": device_id, "profile_id": profile_id}
        )
        if response.get("status") == "usb_started":
            # From now on, attempt stop even if the remainder of the response is invalid.
            started = True
        if (
            not started
            or response.get("firmware_version") != USB_FIRMWARE_VERSION
            or response.get("device_id") != device_id
            or response.get("profile_id") != profile_id
        ):
            raise USBCollectionError("usb_start_identity_or_version_mismatch")
        boot_id, _ = _clock_response(response, device_id)
        _anchor(settings, device_id, response, utc_now())
        deadline = monotonic() + duration
        last_anchor = monotonic()
        if progress:
            progress({"status": "usb_collecting", "firmware_version": USB_FIRMWARE_VERSION})
        while monotonic() < deadline:
            response = exchange({"command": "usb_poll"})
            if response.get("status") == "usb_idle":
                current_boot, _ = _clock_response(response, device_id)
                if current_boot != boot_id:
                    raise USBCollectionError("usb_device_restarted")
                if monotonic() - last_anchor >= 60:
                    _anchor(settings, device_id, response, utc_now())
                    last_anchor = monotonic()
                sleep(min(2.0, max(0.0, deadline - monotonic())))
                continue
            if response.get("status") != "usb_batch":
                raise USBCollectionError("usb_batch_response_invalid")
            batch_id = response.get("batch_id")
            if (
                not isinstance(batch_id, str)
                or not 1 <= len(batch_id) <= 80
                or any(ord(char) < 33 or ord(char) > 126 for char in batch_id)
            ):
                raise USBCollectionError("usb_batch_id_invalid")
            batch = parse_batch(json.dumps(response.get("batch"), allow_nan=False).encode())
            if batch.device_id != device_id:
                raise USBCollectionError("usb_batch_device_mismatch")
            # Old boot/profile records retain their source identity. The storage
            # layer rejects an unregistered profile; it is never relabeled here.
            acknowledgement = storage.ingest(settings.data_dir, device_id, batch)
            rows = acknowledgement["records"]
            summary["accepted_records"] += sum(row["status"] == "accepted" for row in rows)
            summary["duplicate_records"] += sum(row["status"] == "duplicate" for row in rows)
            summary["received_batches"] += 1
            confirmed = exchange(
                {"command": "usb_ack", "batch_id": batch_id, "ack": acknowledgement}
            )
            if confirmed.get("status") != "usb_acked" or confirmed.get("batch_id") != batch_id:
                raise USBCollectionError("usb_ack_not_confirmed_data_kept_on_mac")
            if progress:
                for record in batch.records:
                    if record.record_type == "minute":
                        metrics = record.model_dump(mode="json")["metrics"]
                        progress(
                            {
                                "status": "usb_minute_saved",
                                "boot_id": batch.boot_id,
                                "seq": record.seq,
                                "mono_start_ms": record.mono_start_ms,
                                "metrics": metrics,
                            }
                        )
    except BaseException as exc:
        failure = exc
    finally:
        if start_attempted:
            try:
                summary["device_stopped"] = (
                    exchange({"command": "usb_stop"}).get("status") == "usb_stopped"
                )
            except BaseException:
                summary["device_stopped"] = False
    if not summary["device_stopped"] and start_attempted:
        raise USBCollectionError("usb_stop_unconfirmed_device_may_still_collect") from None
    if failure is not None:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        if isinstance(failure, (USBCollectionError, storage.EnvironmentError)):
            raise failure
        if isinstance(failure, (ValidationError, ValueError, TypeError)):
            raise USBCollectionError("usb_response_invalid") from None
        raise USBCollectionError("usb_collection_interrupted_records_retained") from None
    return summary
