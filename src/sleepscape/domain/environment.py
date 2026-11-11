"""Strict environment wire contract; pure validation and canonical encoding."""

import hashlib
import json
import math
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_BATCH_BYTES = 65_536
MAX_SAFE_INTEGER = 2**53 - 1
Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]
Count = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_INTEGER)]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
QualityCode = Literal[
    "missing",
    "sensor_error",
    "bus_error",
    "crc_error",
    "out_of_range",
    "partial_window",
    "low_light_limit",
    "below_numeric_floor",
    "audio_gap",
    "dma_overflow",
    "clipped",
    "warming",
    "incomplete",
    "merged",
    "time_unknown",
    "ranges_truncated",
]
Quality = Annotated[list[QualityCode], Field(max_length=16)]
State = Literal["available", "partial", "missing", "invalid", "unavailable", "warming"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Channel(Contract):
    state: State
    expected_count: Count
    observed_count: Count
    valid_count: Count
    quality: Quality = Field(default_factory=list)

    @model_validator(mode="after")
    def counts(self):
        if not self.valid_count <= self.observed_count <= self.expected_count:
            raise ValueError("invalid_coverage_counts")
        if self.valid_count == 0 and self.state in {"available", "partial"}:
            raise ValueError("available_requires_valid_observations")
        if self.valid_count > 0 and self.state not in {"available", "partial"}:
            raise ValueError("valid_observations_require_available_state")
        return self


class ScalarChannel(Channel):
    unit: Literal["degC", "percent", "lx"]
    mean: Number | None
    min: Number | None
    max: Number | None

    @model_validator(mode="after")
    def values(self):
        values = (self.mean, self.min, self.max)
        if not self.valid_count:
            if any(value is not None for value in values):
                raise ValueError("missing_values_must_be_null")
        elif any(value is None for value in values) or not self.min <= self.mean <= self.max:
            raise ValueError("invalid_scalar_summary")
        if self.valid_count:
            lower, upper = {"degC": (-80, 125), "percent": (0, 100), "lx": (0, 200_000)}[self.unit]
            if not lower <= self.min <= self.max <= upper:
                raise ValueError("measurement_outside_contract_range")
        return self


class SoundChannel(Channel):
    unit: Literal["dBFS"] = "dBFS"
    energy_mean_dbfs: Number | None
    max_1s_rms_dbfs: Number | None
    expected_samples: Count
    observed_samples: Count
    valid_samples: Count
    valid_duration_ms: Count
    valid_1s_count: Count
    dma_overflow_count: Count
    clipped_samples: Count
    detected_count: Count | None
    stored_count: Annotated[int, Field(strict=True, ge=0, le=1)] | None
    merged_count: Count | None
    dropped_count: Count | None
    event_detector_state: Literal["warming", "ready", "interrupted"]
    event_valid_duration_ms: Count

    @model_validator(mode="after")
    def sound(self):
        if not self.valid_samples <= self.observed_samples <= self.expected_samples:
            raise ValueError("invalid_audio_sample_counts")
        if (
            self.valid_1s_count != self.valid_count
            or self.valid_duration_ms != self.valid_count * 1000
        ):
            raise ValueError("invalid_audio_window_coverage")
        if self.valid_samples < self.valid_count * 16_000:
            raise ValueError("valid_window_requires_full_samples")
        if self.clipped_samples > self.observed_samples:
            raise ValueError("invalid_clipping_count")
        if self.event_valid_duration_ms > self.valid_duration_ms:
            raise ValueError("event_coverage_exceeds_audio_coverage")
        values = (self.energy_mean_dbfs, self.max_1s_rms_dbfs)
        if any(value is not None and not -200 <= value <= 0 for value in values):
            raise ValueError("invalid_dbfs_range")
        if not self.valid_count and any(value is not None for value in values):
            raise ValueError("missing_audio_must_be_null")
        if self.valid_count and any(value is None for value in values):
            if values != (None, None) or "below_numeric_floor" not in self.quality:
                raise ValueError("silent_audio_requires_numeric_floor_quality")
        if all(value is not None for value in values) and values[0] > values[1] + 1e-6:
            raise ValueError("mean_exceeds_maximum")
        counts = (self.detected_count, self.stored_count, self.merged_count, self.dropped_count)
        if self.event_valid_duration_ms == 0:
            if any(value is not None for value in counts):
                raise ValueError("unavailable_detector_counts_must_be_null")
        elif any(value is None for value in counts) or counts[0] != sum(counts[1:]):
            raise ValueError("invalid_event_counts")
        return self


class Metrics(Contract):
    temperature_c: ScalarChannel
    relative_humidity_pct: ScalarChannel
    illuminance_lux: ScalarChannel
    sound_relative: SoundChannel

    @model_validator(mode="after")
    def units(self):
        if (
            self.temperature_c.unit,
            self.relative_humidity_pct.unit,
            self.illuminance_lux.unit,
        ) != ("degC", "percent", "lx"):
            raise ValueError("metric_unit_mismatch")
        return self


class TimedRecord(Contract):
    seq: Count
    measurement_profile_id: Identifier
    mono_start_ms: Count
    mono_end_ms: Count
    quality: Quality = Field(default_factory=list)

    @model_validator(mode="after")
    def window(self):
        if not 0 < self.mono_end_ms - self.mono_start_ms <= 60_000:
            raise ValueError("window_requires_up_to_one_minute")
        return self


class MinuteRecord(TimedRecord):
    record_type: Literal["minute"]
    metrics: Metrics

    @model_validator(mode="after")
    def coverage(self):
        duration = self.mono_end_ms - self.mono_start_ms
        if duration != 60_000 and "partial_window" not in self.quality:
            raise ValueError("partial_window_requires_quality")
        channels = (
            (self.metrics.temperature_c, 10_000),
            (self.metrics.relative_humidity_pct, 10_000),
            (self.metrics.illuminance_lux, 1000),
            (self.metrics.sound_relative, 1000),
        )
        for channel, period in channels:
            expected = (duration + period - 1) // period
            if channel.expected_count != expected:
                raise ValueError("expected_count_does_not_match_window")
        sound = self.metrics.sound_relative
        if sound.expected_samples != duration * 16:
            raise ValueError("audio_expected_samples_mismatch")
        return self


class EventRecord(TimedRecord):
    record_type: Literal["event"]
    event_kind: Literal["relative_sound"] = "relative_sound"
    active_duration_ms: Count
    max_1s_rms_dbfs: Number
    baseline_dbfs: Number | None
    threshold_profile_id: Identifier
    merged: bool
    incomplete: bool
    continued_from_previous: bool = False
    continues_next: bool = False

    @model_validator(mode="after")
    def event(self):
        if not 0 < self.active_duration_ms <= self.mono_end_ms - self.mono_start_ms:
            raise ValueError("event_duration_invalid")
        if not -200 <= self.max_1s_rms_dbfs <= 0:
            raise ValueError("invalid_dbfs_range")
        if self.baseline_dbfs is not None and not -200 <= self.baseline_dbfs <= 0:
            raise ValueError("invalid_dbfs_range")
        if self.baseline_dbfs is None and not self.merged:
            raise ValueError("baseline_required_for_unmerged_event")
        return self


Record = Annotated[MinuteRecord | EventRecord, Field(discriminator="record_type")]


class TimeAnchor(Contract):
    anchor_id: Identifier
    monotonic_ms: Count
    utc_at: Annotated[
        str, StringConstraints(pattern=r"^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")
    ]
    source: Literal["ntp"]
    uncertainty_ms: Number | None
    quality: Quality = Field(default_factory=list)

    @model_validator(mode="after")
    def anchor(self):
        datetime.fromisoformat(self.utc_at)
        if self.uncertainty_ms is not None and self.uncertainty_ms < 0:
            raise ValueError("invalid_time_uncertainty")
        return self


class LostRange(Contract):
    record_type: Literal["minute", "event", "anchor"]
    boot_id: Identifier
    seq_start: Count
    seq_end: Count
    mono_start_ms: Count
    mono_end_ms: Count

    @model_validator(mode="after")
    def ordered(self):
        if self.seq_start > self.seq_end or self.mono_start_ms > self.mono_end_ms:
            raise ValueError("invalid_loss_range")
        return self


class LossCounts(Contract):
    minute: Count = 0
    event: Count = 0
    anchor: Count = 0


class DeviceStatus(Contract):
    status_seq: Count
    monotonic_ms: Count
    queued_records: Count
    quarantined_count: Count = 0
    lost_counts: LossCounts
    lost_ranges: Annotated[list[LostRange], Field(max_length=16)] = Field(default_factory=list)
    ranges_truncated: bool = False
    quality: Quality = Field(default_factory=list)


class EnvironmentBatch(Contract):
    schema_version: Literal[1]
    device_id: Identifier
    boot_id: Identifier
    records: Annotated[list[Record], Field(max_length=120)]
    time_anchors: Annotated[list[TimeAnchor], Field(max_length=60)]
    device_status: DeviceStatus | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version_requires_integer")
        return value

    @model_validator(mode="after")
    def batch(self):
        if not self.records and not self.time_anchors and self.device_status is None:
            raise ValueError("empty_batch")
        for kind in ("minute", "event"):
            rows = [record for record in self.records if record.record_type == kind]
            if len(rows) > 60 or len({row.seq for row in rows}) != len(rows):
                raise ValueError("record_count_or_identity_invalid")
        if len({row.anchor_id for row in self.time_anchors}) != len(self.time_anchors):
            raise ValueError("duplicate_anchor_in_batch")
        return self


class LightMeasurement(Contract):
    """Explicit fixed-gain light conversion; numerical resolution is not accuracy."""

    gain: Annotated[Number, Field(gt=0, le=16)]
    integration_ms: Annotated[int, Field(strict=True, ge=1, le=1000)]
    resolution_lux: Annotated[Number, Field(gt=0, le=100)]
    als_conf: Annotated[int, Field(strict=True, ge=0, le=65535)]
    power_saving_enabled: bool
    auto_range: Literal[False]
    saturation_raw: Annotated[int, Field(strict=True, ge=1, le=65535)]
    range_min_lux: Annotated[Number, Field(ge=0)]
    range_max_lux: Annotated[Number, Field(gt=0)]
    low_light_limit_lux: Annotated[Number, Field(gt=0)]
    conversion: Literal["linear"]
    reference: Identifier

    @field_validator("auto_range", mode="before")
    @classmethod
    def range_mode_requires_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("auto_range_requires_boolean")
        return value

    @model_validator(mode="after")
    def range_matches_conversion(self):
        maximum = (self.saturation_raw - 1) * self.resolution_lux
        if self.range_min_lux != 0 or not math.isclose(self.range_max_lux, maximum, rel_tol=1e-12):
            raise ValueError("light_range_does_not_match_linear_conversion")
        if not self.range_min_lux < self.low_light_limit_lux < self.range_max_lux:
            raise ValueError("light_limit_outside_measurement_range")
        return self


class I2CBus(Contract):
    """Physical sensor connection recorded once per immutable measurement profile."""

    topology: Literal["shared"]
    hub_model: Identifier
    sda_gpio: Annotated[int, Field(strict=True, ge=0, le=48)]
    scl_gpio: Annotated[int, Field(strict=True, ge=0, le=48)]
    frequency_hz: Annotated[int, Field(strict=True, ge=10000, le=1000000)]
    supply_voltage_v: Annotated[Number, Field(gt=0, le=5)]
    temperature_address: Annotated[int, Field(strict=True, ge=8, le=119)]
    light_address: Annotated[int, Field(strict=True, ge=8, le=119)]

    @model_validator(mode="after")
    def independent_lines_and_addresses(self):
        if self.sda_gpio == self.scl_gpio:
            raise ValueError("i2c_data_and_clock_must_differ")
        if self.temperature_address == self.light_address:
            raise ValueError("shared_i2c_sensor_addresses_must_differ")
        return self


# Known released hardware configuration. Native C/profile cross-checks guard this
# contract against firmware changes; future versions retain the general fields above.
_C008_C_0_2_LIGHT = {
    "gain": 2.0,
    "integration_ms": 400,
    "resolution_lux": 0.0084,
    "als_conf": 0x0880,
    "power_saving_enabled": False,
    "auto_range": False,
    "saturation_raw": 65535,
    "range_min_lux": 0.0,
    "range_max_lux": 550.4856,
    "low_light_limit_lux": 0.1,
    "conversion": "linear",
    "reference": "vishay-84286-rev1.8-84323-20250306",
}
_C008_C_0_2_BUS = {
    "topology": "shared",
    "hub_model": "dfrobot-dfr0759",
    "sda_gpio": 21,
    "scl_gpio": 25,
    "frequency_hz": 100000,
    "supply_voltage_v": 3.3,
    "temperature_address": 0x38,
    "light_address": 0x10,
}


def _matches_configuration(actual: dict, expected: dict) -> bool:
    return actual.keys() == expected.keys() and all(
        math.isclose(actual[key], value, rel_tol=1e-12, abs_tol=1e-12)
        if isinstance(value, float)
        else actual[key] == value
        for key, value in expected.items()
    )


class MeasurementProfile(Contract):
    """Immutable operational description; strings are bounded identifiers, not free-form logs."""

    hardware_model: Identifier
    hardware_revision: Identifier
    firmware_version: Identifier
    temperature_sensor: Identifier
    light_sensor: Identifier
    microphone: Identifier
    placement_id: Identifier
    calibration_id: Identifier
    sound_processing: Identifier
    threshold_profile_id: Identifier
    light_measurement: LightMeasurement | None = None
    i2c_bus: I2CBus | None = None
    sample_rate_hz: Literal[16_000] = 16_000
    pcm_bits: Literal[16] = 16
    audio_channels: Literal[1] = 1
    temperature_period_ms: Literal[10_000] = 10_000
    light_period_ms: Literal[1000] = 1000
    aggregation_ms: Literal[60_000] = 60_000
    event_baseline_seconds: Literal[60] = 60
    event_start_delta_db: Literal[10] = 10
    event_stop_delta_db: Literal[6] = 6
    event_stop_seconds: Literal[3] = 3
    event_merge_gap_seconds: Literal[2] = 2

    @model_validator(mode="after")
    def explicit_sensor_configuration(self):
        if self.hardware_model == "m5stack-c008-c" and self.firmware_version == "0.2.0":
            if (
                self.light_sensor != "dfrobot-sen0228-veml7700"
                or self.temperature_sensor != "dfrobot-sen0528-aht20"
                or self.light_measurement is None
                or self.i2c_bus is None
                or not _matches_configuration(
                    self.light_measurement.model_dump(), _C008_C_0_2_LIGHT
                )
                or not _matches_configuration(self.i2c_bus.model_dump(), _C008_C_0_2_BUS)
            ):
                raise ValueError("known_firmware_sensor_configuration_mismatch")
        if self.light_sensor == "dfrobot-sen0228-veml7700":
            if self.light_measurement is None or self.i2c_bus is None:
                raise ValueError("veml7700_requires_measurement_and_bus_profile")
            if self.i2c_bus.light_address != 0x10:
                raise ValueError("veml7700_i2c_address_mismatch")
            if self.light_measurement.integration_ms > self.light_period_ms:
                raise ValueError("light_integration_exceeds_sample_period")
        if self.temperature_sensor == "dfrobot-sen0528-aht20" and self.i2c_bus is not None:
            if self.i2c_bus.temperature_address != 0x38:
                raise ValueError("aht20_i2c_address_mismatch")
        return self

    @model_validator(mode="before")
    @classmethod
    def fixed_parameters_require_integers(cls, values):
        if isinstance(values, dict):
            for name, field in cls.model_fields.items():
                if isinstance(field.default, int) and name in values:
                    if type(values[name]) is not int:
                        raise ValueError("profile_parameter_requires_integer")
        return values


def validate_light_profile(record: Record, profile: MeasurementProfile) -> None:
    """Validate a minute against its declared range; preserve invalid/missing observations."""
    if record.record_type != "minute" or profile.light_measurement is None:
        return
    light = record.metrics.illuminance_lux
    if not light.valid_count:
        return
    specification = profile.light_measurement
    if light.min < specification.range_min_lux or (
        light.max > specification.range_max_lux
        and not math.isclose(light.max, specification.range_max_lux, rel_tol=1e-7, abs_tol=1e-6)
    ):
        raise ValueError("illuminance_outside_measurement_profile")
    # The threshold is a quality hint, not a zero substitute or precision claim.
    expected_low = light.min < specification.low_light_limit_lux
    if ("low_light_limit" in light.quality) != expected_low:
        raise ValueError("light_quality_profile_mismatch")


def canonical_json(value: dict) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )


def content_hash(value: dict) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
