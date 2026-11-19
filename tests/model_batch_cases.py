"""Reproducible synthetic model inputs built through the production readiness pipeline.

This module never opens a database, reads credentials or calls a model. Scenario
labels are test metadata only; the request uses the unmodified project template.
"""

import copy
import math
import random
from datetime import UTC, date, datetime, timedelta

from test_analysis_readiness import lose_samples, sample, stamp

from sleepscape.domain.analysis_readiness import evaluate_readiness
from sleepscape.domain.sleep_features import build_sleep_input
from sleepscape.resources import validate_sleep_input

SCENARIOS = (
    "rest_dark_complete_no_baseline",
    "training_warm_sound_gap_sparse_baseline",
    "stress_light_events_independent_baselines",
    "short_sleep_cool_light_gap",
    "long_sleep_humid_events_full_baseline",
    "dry_room_temperature_gap_no_baseline",
    "lower_hrv_dark_complete_threshold_baseline",
    "exact_99_percent_missing_minutes",
    "sound_events_truncated_full_baseline",
    "five_affected_minutes_sound_gap",
)


def _baselines(night, day, count, rng, *, hrv_missing=False):
    result = []
    for offset in range(1, count + 1):
        when = day - timedelta(days=offset)
        cards = copy.deepcopy(night["cards"])
        for card in cards.values():
            card["source_date"] = when.isoformat()
        cards["sleep_seconds"]["value"] = rng.randint(390, 510) * 60
        cards["average_heart_rate"]["value"] = rng.randint(48, 63)
        cards["night_average_hrv"]["value"] = rng.randint(36, 78)
        if hrv_missing and offset == 1:
            cards["night_average_hrv"].update(value=None, state="missing")
        result.append({"date": when.isoformat(), "cards": cards})
    return result


def _night(index, scenario, rng):
    day = date(2026, 1, 2) + timedelta(days=index)
    minutes = rng.randint(330, 570)
    if scenario == 3:
        minutes = rng.randint(90, 120)
    elif scenario == 4:
        minutes = rng.randint(500, 600)
    elif scenario == 7:
        minutes = 300
    # Shanghai 22:30–23:30 to the following morning; each input is independent.
    start = datetime.combine(day, datetime.min.time(), UTC) - timedelta(
        hours=9, minutes=rng.randint(-30, 30)
    )
    cutoff = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=4)
    night, environment = sample(minutes)
    night.update(date=day.isoformat())
    night["sleep"].update(
        start_at=stamp(start),
        end_at=stamp(start + timedelta(minutes=minutes)),
        stage_timeline_available=False,
    )
    for name, card in night["cards"].items():
        card["source_date"] = (
            day - timedelta(days=int(name in {"training_load", "previous_stress"}))
        ).isoformat()
    night["cards"]["average_heart_rate"]["value"] = rng.randint(46, 75)
    night["cards"]["night_average_hrv"]["value"] = (
        rng.randint(18, 31) if scenario == 6 else rng.randint(30, 95)
    )
    load = 0 if scenario == 0 else rng.randint(15, 220)
    if scenario == 1:
        load = rng.randint(220, 430)
    night["cards"]["training_load"].update(
        value=load,
        activity_count=0 if load == 0 else rng.randint(1, 3),
        reason="no_recorded_activity" if load == 0 else None,
    )
    night["cards"]["previous_stress"]["value"] = (
        rng.randint(50, 75) if scenario == 2 else rng.randint(10, 48)
    )
    # Optional stages are coherent cumulative durations, never stage intervals.
    if index % 3 != 0:
        awake = rng.randint(1, max(2, minutes // 12))
        asleep = minutes - awake
        deep, rem = int(asleep * rng.uniform(0.12, 0.23)), int(asleep * rng.uniform(0.16, 0.27))
        night["cards"]["sleep_seconds"]["value"] = asleep * 60
        night["stages"] = [
            {"stage": name, "seconds": value * 60, "state": "available"}
            for name, value in (
                ("deep", deep),
                ("rem", rem),
                ("light", asleep - deep - rem),
                ("awake", awake),
            )
        ]
    if index % 4 != 0:
        night["cards"]["sleep_score"] = {
            "state": "available",
            "source_date": day.isoformat(),
            "value": rng.randint(45, 95),
        }
        night["cards"]["previous_steps"] = {
            "state": "available",
            "source_date": (day - timedelta(days=1)).isoformat(),
            "value": rng.randint(1800, 24000),
        }
    return day, start, cutoff, night, environment


def _environment(environment, start, scenario, rng):
    minutes = len(environment["records"])
    temperature = rng.uniform(17.5, 25.5)
    if scenario == 1:
        temperature = rng.uniform(27, 30)
    elif scenario == 3:
        temperature = rng.uniform(15, 18)
    humidity = rng.uniform(38, 62)
    if scenario == 4:
        humidity = rng.uniform(68, 78)
    elif scenario == 5:
        humidity = rng.uniform(24, 32)
    light = rng.uniform(0, 0.15) if scenario in {0, 6} else rng.uniform(0.2, 6)
    sound = rng.uniform(-66, -44)
    time_quality = "synced" if scenario % 2 == 0 else "estimated"
    for i, row in enumerate(environment["records"]):
        row.update(
            utc_start=stamp(start + timedelta(minutes=i)),
            utc_end=stamp(start + timedelta(minutes=i + 1)),
            time_quality=time_quality,
            time_uncertainty_ms=30 if time_quality == "synced" else None,
            time_flags=[] if time_quality == "synced" else ["clock_drift_unverified"],
        )
        phase = 2 * math.pi * i / minutes
        values = {
            "temperature_c": temperature + 0.6 * math.sin(phase) + rng.uniform(-0.1, 0.1),
            "relative_humidity_pct": humidity + 2 * math.cos(phase) + rng.uniform(-0.2, 0.2),
            "illuminance_lux": max(0, light + rng.uniform(-0.2, 0.2)),
        }
        if scenario == 2 and minutes // 2 <= i < minutes // 2 + 4:
            values["illuminance_lux"] += rng.uniform(25, 60)
        for name, value in values.items():
            value = round(value, 3)
            row["metrics"][name].update(mean=value, min=value, max=value)
        mean = round(sound + 1.7 * math.sin(phase) + rng.uniform(-1, 1), 3)
        row["metrics"]["sound_relative"].update(
            energy_mean_dbfs=mean,
            max_1s_rms_dbfs=mean + rng.uniform(1, 5),
            stored_count=0,
            dropped_count=0,
        )

    event_count = {2: 2, 4: 1, 8: 10}.get(scenario, 0)
    for number in range(event_count):
        index = (number + 1) * minutes // (event_count + 1)
        row = environment["records"][index]
        peak = rng.uniform(-30, -19)
        row["metrics"]["sound_relative"].update(stored_count=1, max_1s_rms_dbfs=peak)
        left = start + timedelta(minutes=index, seconds=10)
        environment["records"].append(
            {
                "record_type": "event",
                "source_ref": "source-synthetic",
                "boot_ref": "boot-synthetic",
                "time_quality": time_quality,
                "utc_start": stamp(left),
                "utc_end": stamp(left + timedelta(seconds=12)),
                "quality": [],
                "event": {
                    "event_kind": "relative_sound",
                    "active_duration_ms": rng.randint(2, 9) * 1000,
                    "max_1s_rms_dbfs": peak,
                    "merged": number % 2 == 0,
                    "incomplete": False,
                },
            }
        )
    if scenario == 1:
        lose_samples(environment, minutes // 2, "sound_relative", rng.randint(1, 3))
    elif scenario == 3:
        lose_samples(environment, minutes // 2, "illuminance_lux", 1)
    elif scenario == 5:
        lose_samples(environment, minutes // 2, "temperature_c", 1)
    elif scenario == 7:
        del environment["records"][100:103]
    elif scenario == 9:
        for index in range(minutes // 2, minutes // 2 + 5):
            lose_samples(environment, index, "sound_relative", 1)


def generate_cases(seed=20261118, count=50):
    """Return up to the authorized 50 independent, valid synthetic inputs."""
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("invalid_synthetic_seed")
    if type(count) is not int or not 1 <= count <= 50:
        raise ValueError("invalid_synthetic_case_count")
    rng = random.Random(seed)
    cases = []
    for index in range(count):
        scenario = index % len(SCENARIOS)
        day, start, cutoff, night, environment = _night(index, scenario, rng)
        _environment(environment, start, scenario, rng)
        baseline_count = (0, 5, 7, 14, 30, 0, 7, 14, 30, 5)[scenario]
        baseline = _baselines(night, day, baseline_count, rng, hrv_missing=scenario == 2)
        if baseline_count >= 14:
            mean = rng.uniform(30, 160)
            night["load_comparison"] = {
                "state": "available",
                "target_date": (day - timedelta(days=1)).isoformat(),
                "baseline_start": (day - timedelta(days=31)).isoformat(),
                "baseline_end": (day - timedelta(days=2)).isoformat(),
                "baseline_valid_days": 30,
                "baseline_fresh_days": 30,
                "baseline_required_days": 30,
                "baseline_mean": mean,
                "change_percent": (night["cards"]["training_load"]["value"] / mean - 1) * 100,
            }
        readiness = evaluate_readiness(day, night, environment, now=cutoff, lookback_days=31)
        payload = build_sleep_input(
            day,
            night,
            environment,
            readiness,
            baseline_nights=baseline,
            data_cutoff_at=cutoff,
            synthetic=True,
        )
        validate_sleep_input(payload)
        cases.append(
            {
                "case_id": f"synthetic-{index + 1:03}",
                "scenario": SCENARIOS[scenario],
                "payload": payload,
            }
        )
    return cases
