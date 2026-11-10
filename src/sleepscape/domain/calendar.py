"""Calendar operations shared by collection plans and offline evaluation."""

from datetime import date, timedelta


def dates(start: date, end: date) -> list[date]:
    """Return inclusive calendar dates; a reversed range has no dates."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
