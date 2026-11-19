"""Presentation dates never change collection windows or stored observations."""

from datetime import date


class NightBeforeDisplayStart(ValueError):
    """The requested wake-up date is outside the configured dashboard window."""


def visible_start(day: date, start_date: date | None) -> date:
    return max(day, start_date) if start_date is not None else day


def require_visible_night(day: date, start_date: date | None) -> None:
    if start_date is not None and day < start_date:
        raise NightBeforeDisplayStart("night_before_dashboard_start")
