"""Pure, conservative UTC mapping for observations from one device boot.

The caller groups anchors by device and boot before building a mapper. A mapper
holds one fixed offset, so adjacent monotonic windows remain adjacent even when
different anchors are nearer to them. Rebuild it when the boot's evidence changes.

The two-second conflict tolerance is an engineering jump detector, not an accuracy
claim. A conflict rejects the whole boot until a more selective, independently
validated mapping is available. No oscillator drift model has been validated, so
even NTP-based intervals are estimates with unknown total uncertainty.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sleepscape.domain.environment import MAX_SAFE_INTEGER, TimeAnchor

TIME_MAPPING_ALGORITHM = "boot-fixed-offset-1"
ANCHOR_CONFLICT_TOLERANCE_MS = 2000
TIME_MAPPING_QUALITY_CODES = frozenset(
    {
        "anchor_quality_rejected",
        "no_usable_time_anchor",
        "anchor_conflict",
        "whole_boot_conflict",
        "clock_drift_unverified",
        "utc_out_of_range",
    }
)
TimeQuality = Literal["synced", "estimated", "unknown", "conflict"]

_UTC_ORIGIN = datetime(1970, 1, 1, tzinfo=UTC)
_UTC_MINIMUM = datetime(2000, 1, 1, tzinfo=UTC)
_UTC_LIMIT = datetime(2100, 1, 1, tzinfo=UTC)
MIN_UTC_US = (_UTC_MINIMUM - _UTC_ORIGIN).days * 86_400_000_000
MAX_UTC_US = (_UTC_LIMIT - _UTC_ORIGIN).days * 86_400_000_000


@dataclass(frozen=True, slots=True)
class TimeMapping:
    utc_start: str | None
    utc_end: str | None
    time_quality: TimeQuality
    time_uncertainty_ms: float | None
    anchor_ids: tuple[str, ...]
    quality: tuple[str, ...]
    mapping_algorithm: str = TIME_MAPPING_ALGORITHM


@dataclass(frozen=True, slots=True)
class BootTimeMapper:
    """An immutable summary; mapping a half-open interval takes constant work."""

    _offset_us: int | None
    time_quality: TimeQuality
    anchor_ids: tuple[str, ...]
    quality: tuple[str, ...]
    mapping_algorithm: str = TIME_MAPPING_ALGORITHM

    def to_dict(self) -> dict:
        """A JSON-compatible cache; it contains no original anchor payloads."""
        return {
            "_offset_us": self._offset_us,
            "time_quality": self.time_quality,
            "anchor_ids": list(self.anchor_ids),
            "quality": list(self.quality),
            "mapping_algorithm": self.mapping_algorithm,
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> "BootTimeMapper":
        """Restore a bounded summary, rejecting corrupt or obsolete cache formats."""
        try:
            if set(value) != {
                "_offset_us",
                "time_quality",
                "anchor_ids",
                "quality",
                "mapping_algorithm",
            }:
                raise ValueError
            offset = value["_offset_us"]
            state = value["time_quality"]
            anchors, quality = value["anchor_ids"], value["quality"]
            if (
                value["mapping_algorithm"] != TIME_MAPPING_ALGORITHM
                or state not in {"estimated", "unknown", "conflict"}
                or type(anchors) not in {list, tuple}
                or type(quality) not in {list, tuple}
                or len(anchors) > 3
                or len(quality) > 16
                or any(not isinstance(item, str) or not 1 <= len(item) <= 64 for item in anchors)
                or any(not isinstance(item, str) or not 1 <= len(item) <= 64 for item in quality)
                or not set(quality) <= TIME_MAPPING_QUALITY_CODES
                or len(set(anchors)) != len(anchors)
                or len(set(quality)) != len(quality)
            ):
                raise ValueError
            if state == "estimated":
                if (
                    type(offset) is not int
                    or not MIN_UTC_US - MAX_SAFE_INTEGER * 1000 <= offset < MAX_UTC_US
                    or len(anchors) != 1
                ):
                    raise ValueError
            elif (
                offset is not None
                or (state == "unknown" and anchors)
                or (state == "conflict" and not anchors)
            ):
                raise ValueError
            return cls(offset, state, tuple(anchors), tuple(quality))
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid_time_mapper_cache") from None

    def map_interval(self, start_ms: int, end_ms: int) -> TimeMapping:
        """Map [start_ms, end_ms), never substitute a receive time or another boot."""
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or not 0 <= start_ms < end_ms <= MAX_SAFE_INTEGER
        ):
            raise ValueError("invalid_monotonic_interval")
        if self._offset_us is None:
            return TimeMapping(None, None, self.time_quality, None, self.anchor_ids, self.quality)
        start_us = self._offset_us + start_ms * 1000
        end_us = self._offset_us + end_ms * 1000
        # An interval may end at 2100-01-01: its right endpoint is exclusive.
        if not MIN_UTC_US <= start_us < end_us <= MAX_UTC_US:
            return TimeMapping(
                None,
                None,
                "unknown",
                None,
                self.anchor_ids,
                (*self.quality, "utc_out_of_range"),
            )
        return TimeMapping(
            us_to_utc(start_us),
            us_to_utc(end_us),
            self.time_quality,
            None,
            self.anchor_ids,
            self.quality,
        )


@dataclass(frozen=True, slots=True)
class _Anchor:
    anchor_id: str
    monotonic_ms: int
    utc_us: int
    uncertainty_us: int
    source: str

    @property
    def offset_us(self) -> int:
        return self.utc_us - self.monotonic_ms * 1000

    @property
    def order(self) -> tuple[int, str, int]:
        return self.monotonic_ms, self.anchor_id, self.utc_us


def utc_to_us(value: str | datetime) -> int:
    """Convert an explicitly zoned UTC instant to exact Unix epoch microseconds."""
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise ValueError
        at = at.astimezone(UTC)
        if not _UTC_MINIMUM <= at <= _UTC_LIMIT:
            raise ValueError
        delta = at - _UTC_ORIGIN
        return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, TypeError, ValueError):
        raise ValueError("invalid_environment_utc") from None


def us_to_utc(value_us: int) -> str:
    """Convert exact epoch microseconds; 2100-01-01 is a permitted right endpoint."""
    if type(value_us) is not int or not MIN_UTC_US <= value_us <= MAX_UTC_US:
        raise ValueError("invalid_environment_utc")
    return (_UTC_ORIGIN + timedelta(microseconds=value_us)).isoformat().replace("+00:00", "Z")


def _snapshot(anchor: TimeAnchor) -> _Anchor:
    """Copy scalar evidence so later mutation of a wire DTO cannot alter a mapper."""
    try:
        if (
            not isinstance(anchor, TimeAnchor)
            or type(anchor.monotonic_ms) is not int
            or not 0 <= anchor.monotonic_ms <= MAX_SAFE_INTEGER
            or not isinstance(anchor.anchor_id, str)
            or not anchor.anchor_id
            or anchor.source not in {"ntp", "host_usb"}
            or not isinstance(anchor.utc_at, str)
            or not anchor.utc_at.endswith("Z")
        ):
            raise ValueError
        at = datetime.fromisoformat(anchor.utc_at)
        if not _UTC_MINIMUM <= at < _UTC_LIMIT:
            raise ValueError
        utc_us = utc_to_us(at)
        uncertainty = anchor.uncertainty_ms
        if uncertainty is None:
            # Unknown accuracy does not license an arbitrary clock jump. Zero here
            # is only an absent allowance in the consistency heuristic; it is never
            # returned as a measurement uncertainty.
            uncertainty_us = 0
        else:
            if type(uncertainty) not in {int, float} or not math.isfinite(uncertainty):
                raise ValueError
            if uncertainty < 0:
                raise ValueError
            # Avoid overflowing a float when converting a large finite allowance.
            numerator, denominator = uncertainty.as_integer_ratio()
            uncertainty_us = (numerator * 1000 + denominator - 1) // denominator
        return _Anchor(anchor.anchor_id, anchor.monotonic_ms, utc_us, uncertainty_us, anchor.source)
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError("invalid_time_anchor") from None


def build_time_mapper(anchors: Sequence[TimeAnchor]) -> BootTimeMapper:
    """Build an O(1)-size summary in one pass over a single boot's anchors.

    A pair conflicts when its offsets differ by more than the engineering
    tolerance plus both declared uncertainty allowances. Comparing the greatest
    lower bound and least upper bound detects every such pair, including two
    precise anchors masked by a less precise reference anchor.

    Anchors carrying any quality flag cannot establish UTC. The current wire
    contract has ``time_unknown`` but no separately validated usable clock flags;
    ignoring an unknown quality warning would manufacture confidence.
    """
    reference: _Anchor | None = None
    greatest_lower: tuple[int, _Anchor] | None = None
    least_upper: tuple[int, _Anchor] | None = None
    rejected_quality = False
    for item in anchors:
        anchor = _snapshot(item)
        if item.quality:
            rejected_quality = True
            continue
        if reference is None or anchor.order < reference.order:
            reference = anchor
        lower = anchor.offset_us - anchor.uncertainty_us
        upper = anchor.offset_us + anchor.uncertainty_us
        if (
            greatest_lower is None
            or lower > greatest_lower[0]
            or (lower == greatest_lower[0] and anchor.order < greatest_lower[1].order)
        ):
            greatest_lower = (lower, anchor)
        if (
            least_upper is None
            or upper < least_upper[0]
            or (upper == least_upper[0] and anchor.order < least_upper[1].order)
        ):
            least_upper = (upper, anchor)
    quality = ("anchor_quality_rejected",) if rejected_quality else ()
    if reference is None:
        return BootTimeMapper(None, "unknown", (), (*quality, "no_usable_time_anchor"))
    assert greatest_lower is not None and least_upper is not None
    if greatest_lower[0] > least_upper[0] + ANCHOR_CONFLICT_TOLERANCE_MS * 1000:
        witnesses = sorted((greatest_lower[1], least_upper[1]), key=lambda item: item.order)
        anchor_ids = tuple(dict.fromkeys([reference.anchor_id, *(a.anchor_id for a in witnesses)]))
        return BootTimeMapper(
            None,
            "conflict",
            anchor_ids,
            (*quality, "anchor_conflict", "whole_boot_conflict"),
        )
    return BootTimeMapper(
        reference.offset_us,
        "estimated",
        (reference.anchor_id,),
        (*quality, "clock_drift_unverified"),
    )


def summarize_intervals(intervals: Iterable[tuple[int, int]], start_us: int, end_us: int) -> dict:
    """Clip half-open intervals, then report exact union, gaps and real overlaps.

    Endpoints must use the same coordinate system, normally Unix microseconds.
    An identical interval still counts twice for overlap detection; it never adds
    coverage twice. ``duplicate_interval_count`` counts additional equal intervals,
    not duplicate record identities. Device/boot/profile grouping belongs to the
    caller, as does any claim about individual channel completeness.
    """
    if type(start_us) is not int or type(end_us) is not int or start_us >= end_us:
        raise ValueError("invalid_interval_window")
    events = {start_us: 0, end_us: 0}
    seen = set()
    duplicates = count = 0
    for interval in intervals:
        try:
            left, right = interval
        except (TypeError, ValueError):
            raise ValueError("invalid_coverage_interval") from None
        if type(left) is not int or type(right) is not int or left >= right:
            raise ValueError("invalid_coverage_interval")
        begin, end = max(left, start_us), min(right, end_us)
        if begin >= end:
            continue
        count += 1
        identity = left, right
        duplicates += identity in seen
        seen.add(identity)
        events[begin] = events.get(begin, 0) + 1
        events[end] = events.get(end, 0) - 1
    gaps: list[tuple[int, int]] = []
    overlaps: list[tuple[int, int]] = []
    active = covered_us = overlap_us = 0
    previous = start_us

    def append_segment(target: list[tuple[int, int]], left: int, right: int) -> None:
        if target and target[-1][1] == left:
            target[-1] = target[-1][0], right
        else:
            target.append((left, right))

    for point, delta in sorted(events.items()):
        if point > previous:
            if active:
                covered_us += point - previous
            else:
                append_segment(gaps, previous, point)
            if active > 1:
                overlap_us += point - previous
                append_segment(overlaps, previous, point)
        active += delta
        previous = point
    return {
        "gaps": gaps,
        "overlaps": overlaps,
        "covered_us": covered_us,
        "overlap_us": overlap_us,
        "interval_count": count,
        "duplicate_interval_count": duplicates,
    }
