"""Synthetic, disposable browser fixture. Never opens the user's data or credentials."""

import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from sleepscape.app import create_app
from sleepscape.config import Settings
from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.storage import Repository

END = date(2026, 11, 9)


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


if __name__ == "__main__":
    with TemporaryDirectory(prefix="sleepscape-ui-synthetic-") as directory:
        root = Path(directory)
        seed(root / "data")
        settings = Settings(
            _env_file=None,
            data_dir=root / "data",
            demo=True,
            garmin_key_file=root / "unused-key",
            garmin_token_cache=root / "unused-token",
        )
        uvicorn.run(create_app(settings), host="127.0.0.1", port=8766, access_log=False)
