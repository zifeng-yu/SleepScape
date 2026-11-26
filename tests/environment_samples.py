"""Synthetic room measurements use the same instrument configuration as firmware registration."""

import json
from pathlib import Path

from sleepscape.domain.environment import MeasurementProfile

PROFILE_TEMPLATE = Path(__file__).resolve().parents[1] / "firmware/profile.example.json"


def sample_profile():
    fields = json.loads(PROFILE_TEMPLATE.read_bytes())
    fields["placement_id"] = "synthetic-bedside"
    return MeasurementProfile.model_validate(fields)
