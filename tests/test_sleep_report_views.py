"""Read-only report presentation keeps published evidence through current-data failures."""

import json
from datetime import date

import pytest

from sleepscape.config import Settings
from sleepscape.services import analysis_views
from sleepscape.storage.reader import StorageReadError

DAY = date(2026, 1, 1)


def ready():
    return {
        "policy_version": "dual-source-coverage-1",
        "policy": {"min_channel_coverage": 0.99, "max_affected_span_seconds": 300},
        "quality_state": "with_gaps",
        "warnings": [],
        "night_date": str(DAY),
        "checked_at": "2026-01-01T01:00:00Z",
        "data_state": "ready",
        "core_ready": True,
        "missing": [],
        "optional_missing": [],
        "environment_summary": {},
        "garmin_summary": {},
        "limitations": [],
        "window": None,
        "source_fingerprint": "private-source-fingerprint",
    }


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        analysis_views, "model_status", lambda _: {"configuration_state": "disabled"}
    )
    return Settings(_env_file=None, data_dir=tmp_path / "data")


def test_report_view_passes_private_readiness_for_comparison_but_never_serializes_it(
    settings, monkeypatch
):
    current = ready()
    monkeypatch.setattr(analysis_views, "evaluate_night", lambda *_: current)

    def report_state(settings, day, *, readiness):
        assert readiness is current
        assert readiness["source_fingerprint"] == "private-source-fingerprint"
        return {"execution_state": "queued", "report": None, "blockers": [], "attempt": None}

    monkeypatch.setattr(analysis_views, "report_state", report_state)
    view = analysis_views.analysis_view(settings, DAY)
    assert view["implementation_stage"] == "reports"
    assert view["execution_state"] == "queued"
    assert view["data_state"] == "ready"
    assert "source_fingerprint" not in json.dumps(view)
    assert "private-source-fingerprint" not in json.dumps(view)
    assert view["readiness_unavailable"] is False
    assert not settings.data_dir.exists()


def test_report_view_keeps_published_report_when_current_data_cannot_be_read(settings, monkeypatch):
    def unavailable(*_):
        raise StorageReadError("private database path must not be public")

    monkeypatch.setattr(analysis_views, "evaluate_night", unavailable)
    saved = {"night_date": str(DAY), "source_changed": None, "content": {"status": "complete"}}

    def report_state(settings, day, *, readiness):
        assert readiness is None
        return {"execution_state": "complete", "report": saved, "blockers": []}

    monkeypatch.setattr(analysis_views, "report_state", report_state)
    view = analysis_views.analysis_view(settings, DAY)
    assert view["report"] is saved
    assert view["execution_state"] == "complete"
    assert view["readiness"] is None
    assert view["readiness_unavailable"] is True
    assert view["data_state"] == "not_evaluated"
    assert view["missing"] == []
    assert "private database" not in json.dumps(view)
    assert not settings.data_dir.exists()


def test_hidden_night_never_reads_report_or_readiness(settings, monkeypatch):
    settings.dashboard_start_date = date(2026, 1, 2)
    monkeypatch.setattr(
        analysis_views, "evaluate_night", lambda *_: pytest.fail("hidden date read")
    )
    monkeypatch.setattr(
        analysis_views, "report_state", lambda *_: pytest.fail("hidden report read")
    )
    with pytest.raises(ValueError):
        analysis_views.analysis_view(settings, DAY)
    assert not settings.data_dir.exists()
