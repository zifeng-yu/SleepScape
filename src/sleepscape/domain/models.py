"""Shared contracts between collection, normalization and persistence."""

from dataclasses import dataclass, field
from typing import Any

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


@dataclass(frozen=True)
class DataRequest:
    resource: str
    key: str
    path: str
    params: dict[str, str | int] = field(default_factory=dict)
    start_date: str | None = None
    end_date: str | None = None


@dataclass
class NormalizedBatch:
    # Record: kind, key, date (ISO local date or None), timestamp (UTC ISO or None), data.
    records: list[dict[str, Any]] = field(default_factory=list)
    # Issue: severity (error/warning/info), code, field; no response values or user identifiers.
    issues: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchResult:
    request: DataRequest
    payload: JsonValue
    fetched_at: str
    http_status: int
