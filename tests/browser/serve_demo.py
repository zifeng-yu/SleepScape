"""Synthetic, disposable browser fixture. Never opens the user's data or credentials."""

import copy
import json
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.domain.environment import EnvironmentBatch, MeasurementProfile
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.storage import Repository
from sleepscape.storage import environment as environment_storage
from sleepscape.storage.environment_index import migrate_index, rebuild_index

END = date(2026, 11, 9)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def record(kind, day, values, key=None, instant=None):
    return {
        "kind": kind,
        "key": key or day.isoformat(),
        "date": day.isoformat(),
        "timestamp": instant,
        "data": {"quality": {}, "source": {}, **values},
    }


def seed(directory: Path):
    now = datetime.now(UTC).isoformat()
    with Repository(directory) as repository:
        run = repository.start_run(
            {
                "end_date": END.isoformat(),
                "detail_start": "2026-11-08",
                "history_start": "2026-10-02",
            }
        )

        def save(resource, first, last, rows, pagination=False):
            request = DataRequest(
                resource,
                f"{first}:{last}",
                "/synthetic",
                start_date=first.isoformat(),
                end_date=last.isoformat(),
            )
            repository.save_snapshot(
                run,
                FetchResult(request, {"synthetic": True}, now, 200),
                NormalizedBatch(rows, metrics={"pagination_complete": pagination}),
            )

        daily, activities = [], []
        for index in range(39):
            day = END - timedelta(days=38 - index)
            daily.append(
                record(
                    "daily_summary",
                    day,
                    {"average_stress": 20 + index % 10, "steps": 5200 + index * 15},
                )
            )
            if index % 3 != 0:
                begin = datetime.combine(day, time(9), UTC)
                activities.append(
                    record(
                        "activity",
                        day,
                        {
                            "sport_type": "running",
                            "start_at": begin.isoformat(),
                            "end_at": (begin + timedelta(minutes=35)).isoformat(),
                            "training_load": 85 + index,
                            "duration_seconds": 2100,
                            "average_heart_rate": 132,
                            "date_basis": "source_local_start",
                            "ownerDisplayName": "PRIVATE_MARKER_MUST_NOT_REACH_BROWSER",
                            "startLatitude": 9999,
                        },
                        key=f"synthetic-{index}",
                        instant=begin.isoformat(),
                    )
                )
        save("daily_summaries", END - timedelta(days=38), END, daily)
        save("activities", END - timedelta(days=38), END, activities, True)

        for offset in (1, 0):
            day = END - timedelta(days=offset)
            start = datetime.combine(day - timedelta(days=1), time(15, 10), UTC)
            finish = start + timedelta(hours=7, minutes=40)
            nap_start = datetime.combine(day, time(5), UTC)
            sleep = record(
                "sleep",
                day,
                {
                    "start_at": start.isoformat(),
                    "end_at": finish.isoformat(),
                    "sleep_seconds": 26700 - offset * 600,
                    "deep_seconds": 5100,
                    "light_seconds": 15900,
                    "rem_seconds": 5700,
                    "awake_seconds": 900,
                    "unmeasurable_seconds": 0,
                    "average_heart_rate": 56 + offset,
                    "average_respiration": 14.2,
                    "sleep_score": 83 - offset * 2,
                    "stage_timeline_available": False,
                    "confirmed": True,
                },
                instant=start.isoformat(),
            )
            nap = record(
                "nap",
                day,
                {
                    "start_at": nap_start.isoformat(),
                    "end_at": (nap_start + timedelta(minutes=20)).isoformat(),
                    "duration_seconds": 1200,
                },
                key="nap-" + day.isoformat(),
                instant=nap_start.isoformat(),
            )
            save("sleep", day, day, [sleep, nap])
            hrv = [
                record("hrv_daily", day, {"night_average_ms": 63 + offset, "weekly_average_ms": 60})
            ]
            hr = []
            for index in range(231):
                timestamp = (start + timedelta(minutes=index * 2)).isoformat()
                bpm = round(56 + 4 * math.sin(index / 13) + 2 * math.cos(index / 5), 1)
                hr.append(
                    record(
                        "heart_rate_sample",
                        day - timedelta(days=1),
                        {"bpm": None if 50 < index < 64 else bpm, "series_type": "native"},
                        key="native:" + timestamp,
                        instant=timestamp,
                    )
                )
            for index in range(93):
                timestamp = (start + timedelta(minutes=index * 5)).isoformat()
                hrv.append(
                    record(
                        "hrv_sample",
                        day,
                        {"hrv_ms": 63 + 8 * math.sin(index / 8)},
                        key=timestamp,
                        instant=timestamp,
                    )
                )
            save("heart_rate", day - timedelta(days=1), day - timedelta(days=1), hr)
            save("hrv", day, day, hrv)
            save(
                "daily",
                day,
                day,
                [
                    record(
                        "daily_summary_legacy",
                        day,
                        {
                            "body_battery_at_wake": 75,
                            "body_battery_during_sleep": 47,
                            "last_sync_at": now,
                        },
                    )
                ],
            )
            spo2, battery = [], []
            for index in range(8):
                stamp = (start + timedelta(hours=index)).isoformat()
                spo2.append(
                    record(
                        "spo2_sample",
                        day - timedelta(days=1),
                        {
                            "percent": 96 + index % 3,
                            "series_type": "hourly_average",
                            "aggregation_window_seconds": 3600,
                        },
                        key="hourly_average:" + stamp,
                        instant=stamp,
                    )
                )
            for index in range(3):
                stamp = (start + timedelta(hours=index * 3)).isoformat()
                battery.append(
                    record(
                        "body_battery_sample",
                        day - timedelta(days=1),
                        {
                            "level": 24 + index * 19,
                            "series_type": "native",
                        },
                        key="native:" + stamp,
                        instant=stamp,
                    )
                )
            save("spo2", day - timedelta(days=1), day - timedelta(days=1), spo2)
            save("body_battery", day - timedelta(days=1), day - timedelta(days=1), battery)
        repository.finish_run(run, "completed", {"status": "completed", "failed_resources": 0})


def seed_environment(directory: Path):
    """Exercise production contracts/storage/indexing using only synthetic observations."""
    profile = MeasurementProfile.model_validate_json(
        (FIXTURES / "environment-profile.json").read_bytes()
    )
    template = json.loads((FIXTURES / "environment-batch.json").read_bytes())
    device = "browser-private-synthetic-device"
    initial = "browser-private-profile-a"
    moved = "browser-private-profile-b"
    environment_storage.register_local(directory, device, initial, profile)
    environment_storage.add_profile(
        directory,
        device,
        moved,
        profile.model_copy(update={"placement_id": "browser-private-moved-placement"}),
    )

    def scalar(channel, mean, spread):
        channel.update(mean=mean, min=mean - spread, max=mean + spread, quality=[])

    def minute(index, profile_id):
        result = copy.deepcopy(template["records"][0])
        result.update(
            seq=index,
            mono_start_ms=index * 60000,
            mono_end_ms=(index + 1) * 60000,
            measurement_profile_id=profile_id,
        )
        metrics = result["metrics"]
        scalar(metrics["temperature_c"], round(22 + math.sin(index / 45), 2), 0.2)
        scalar(metrics["relative_humidity_pct"], round(51 + math.sin(index / 20) * 2, 2), 1.0)
        if index % 90 == 15:
            scalar(metrics["illuminance_lux"], 12.6, 2.1)
        sound = metrics["sound_relative"]
        sound.update(
            energy_mean_dbfs=round(-47 + math.sin(index / 15) * 3, 2),
            max_1s_rms_dbfs=round(-37 + math.sin(index / 15) * 3, 2),
            event_detector_state="ready",
            event_valid_duration_ms=60000,
            detected_count=0,
            stored_count=0,
            merged_count=0,
            dropped_count=0,
            quality=[],
        )
        if index == 0:
            sound.update(
                event_detector_state="warming",
                event_valid_duration_ms=0,
                detected_count=None,
                stored_count=None,
                merged_count=None,
                dropped_count=None,
                quality=["warming"],
            )
        if index == 40:
            metrics["relative_humidity_pct"].update(
                state="missing",
                observed_count=0,
                valid_count=0,
                mean=None,
                min=None,
                max=None,
                quality=["missing"],
            )
        if index == 50:
            metrics["illuminance_lux"].update(
                state="invalid",
                valid_count=0,
                mean=None,
                min=None,
                max=None,
                quality=["out_of_range"],
            )
        if index == 60:
            sound.update(
                state="partial",
                observed_count=55,
                valid_count=55,
                observed_samples=880000,
                valid_samples=880000,
                valid_duration_ms=55000,
                valid_1s_count=55,
                dma_overflow_count=1,
                event_detector_state="interrupted",
                event_valid_duration_ms=50000,
                quality=["audio_gap", "dma_overflow"],
            )
        if index == 70:
            sound.update(
                energy_mean_dbfs=None,
                max_1s_rms_dbfs=None,
                quality=["below_numeric_floor"],
            )
        if index == 120:
            sound.update(detected_count=3, stored_count=1, merged_count=2, max_1s_rms_dbfs=-28.0)
        return result

    def put(boot, rows, anchors, status=None):
        payload = {
            "schema_version": 1,
            "device_id": device,
            "boot_id": boot,
            "records": rows,
            "time_anchors": anchors,
            "device_status": status,
        }
        # Keep fixture batches within the same HTTP wire limit, even though this
        # fixture deliberately uses the local USB-style storage entrypoint.
        assert len(json.dumps(payload).encode()) <= 65536
        return environment_storage.ingest(
            directory, device, EnvironmentBatch.model_validate(payload)
        )

    def anchor(stamp, *, identity="0", mono=0):
        return {
            "anchor_id": identity,
            "monotonic_ms": mono,
            "utc_at": stamp.isoformat().replace("+00:00", "Z"),
            "source": "host_usb",
            "uncertainty_ms": None,
            "quality": [],
        }

    # 11/07 has no Garmin sleep, so it exercises the same fallback time window
    # with genuine environment observations rather than a fabricated sleep.
    for offset in (2, 1, 0):
        day = END - timedelta(days=offset)
        start = datetime.combine(day - timedelta(days=1), time(15, 10), UTC)
        for phase, profile_id in enumerate((initial, moved)):
            boot = f"browser-private-boot-{day.isoformat()}-{phase}"
            begin = start + timedelta(minutes=phase * 230)
            rows = [minute(index, profile_id) for index in range(230) if index != 30]
            event = {
                "record_type": "event",
                "seq": 0,
                "measurement_profile_id": profile_id,
                "mono_start_ms": 120 * 60000,
                "mono_end_ms": 121 * 60000,
                "quality": ["merged"],
                "event_kind": "relative_sound",
                "active_duration_ms": 2000,
                "max_1s_rms_dbfs": -28.0,
                "baseline_dbfs": None,
                "threshold_profile_id": profile.threshold_profile_id,
                "merged": True,
                "incomplete": False,
            }
            for cursor in range(0, len(rows), 25):
                put(boot, rows[cursor : cursor + 25], [anchor(begin)] if cursor == 0 else [])
            put(boot, [event], [])
            snapshot = copy.deepcopy(template["device_status"])
            snapshot.update(monotonic_ms=230 * 60000, queued_records=12)
            put(boot, [], [], snapshot)
            # Re-delivery must not duplicate plotted points or events.
            duplicate = put(boot, [rows[0]], [])
            assert duplicate["records"][0]["status"] == "duplicate"

    unknown = minute(0, initial)
    put("browser-private-unknown-boot", [unknown], [])
    begin = datetime(2026, 11, 8, 15, 10, tzinfo=UTC)
    put(
        "browser-private-conflict-boot",
        [minute(0, initial)],
        [
            anchor(begin),
            anchor(begin + timedelta(minutes=1, seconds=10), identity="later", mono=60000),
        ],
    )
    afternoon = [minute(index, initial) for index in range(10)]
    for row in afternoon:
        scalar(row["metrics"]["temperature_c"], -5.0, 0.2)
        row["metrics"]["sound_relative"].update(energy_mean_dbfs=-120.0, max_1s_rms_dbfs=-100.0)
    put(
        "browser-private-afternoon-only-boot",
        afternoon,
        [anchor(datetime(2026, 11, 10, 6, tzinfo=UTC))],
    )
    # This database is disposable. Explicit indexing mirrors the P2 production
    # operations without introducing real-data migration into GET or startup.
    migrate_index(directory)
    rebuild_index(directory)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="sleepscape-ui-synthetic-") as directory:
        # macOS exposes the temporary root through /var -> /private/var. Use its
        # real directory so the model state reader can keep rejecting symlinks.
        root = Path(directory).resolve()
        seed(root / "data")
        seed_environment(root / "data")
        settings = Settings(
            _env_file=None,
            data_dir=root / "data",
            demo=True,
            dashboard_start_date=None,
            garmin_key_file=root / "unused-key",
            garmin_token_cache=root / "unused-token",
            llm_enabled=False,
            llm_base_url=None,
            llm_endpoint=None,
            llm_key_file=root / "unused-model-key",
        )
        uvicorn.run(create_app(settings), host="127.0.0.1", port=8766, access_log=False)
