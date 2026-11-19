"""Personal morning ratings, independent of device measurements."""

from enum import StrEnum


class Feeling(StrEnum):
    POOR = "poor"
    FAIR = "fair"
    GOOD = "good"
