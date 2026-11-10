"""Collection orchestration against synthetic gateways; never authenticate or send traffic."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import requests

from sleepscape.adapters.catalog import daily_request, range_request
from sleepscape.adapters.garmin import GarminError
from sleepscape.config import Settings
from sleepscape.domain.models import FetchResult
from sleepscape.services.collection import CollectionError, collect
from sleepscape.storage import Repository

END = date(2026, 1, 31)


@pytest.fixture(autouse=True)
def no_network_or_account(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Synthetic service tests must not access the network or real account")

    monkeypatch.setattr(requests.Session, "send", fail)
    monkeypatch.setattr("sleepscape.services.collection.GarminGateway", fail)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_PERSONAL_API_BASE_URL", raising=False)
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        timezone="Asia/Shanghai",
        garmin_key_file=tmp_path / "unused-key",
        garmin_token_cache=tmp_path / "unused-token",
    )


class SyntheticGateway:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def fetch(self, request):
        self.calls.append(request)
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return FetchResult(request, result, "2026-02-01T00:00:00Z", 200)


def activity(identity, load=10):
    return {
        "activityId": identity,
        "activityType": {"typeKey": "running"},
        "startTimeGMT": "2026-01-30T23:00:00Z",
        "startTimeLocal": "2026-01-31T07:00:00",
        "elapsedDuration": 60,
        "duration": 60,
        "activityTrainingLoad": load,
    }


def only_activities(monkeypatch):
    req = range_request("activities", date(2026, 1, 1), END)
    req = replace(req, params={**req.params, "limit": 2})
    monkeypatch.setattr("sleepscape.services.collection.build_plan", lambda *args: [req])
    return req


def test_complete_pagination_is_atomic_and_repeated_collection_is_idempotent(settings, monkeypatch):
    only_activities(monkeypatch)
    outcomes = [[activity(1), activity(2)], []]
    first_gateway = SyntheticGateway(outcomes)
    first = collect(settings, END, gateway=first_gateway)
    assert first["status"] == "completed"
    assert first["integrity"] == ["ok"]
    assert [r.params["start"] for r in first_gateway.calls] == [0, 2]
    assert all(r.resource == "activities_page" for r in first_gateway.calls)
    with Repository(settings.data_dir) as repo:
        assert len(repo.list_records(resource="activities")) == 2
        assert repo.list_records(resource="activities_page") == []
        assert [r["status"] for r in repo.list_requests(first["run_id"])] == ["ok", "empty", "ok"]
        first_counts = repo.counts()
        assert first_counts["raw_snapshots"] == 3
    second = collect(settings, END, gateway=SyntheticGateway(outcomes))
    with Repository(settings.data_dir) as repo:
        assert repo.counts()["raw_snapshots"] == first_counts["raw_snapshots"]
        assert len(repo.list_records(resource="activities")) == 2
        assert repo.counts()["requests"] == 6
        assert repo.latest_run()["id"] == second["run_id"]
    assert second["new_snapshots"] == 0


@pytest.mark.parametrize(
    ("second_page", "failure_code"),
    [
        (GarminError("retry_exhausted", 503), "retry_exhausted"),
        ([activity(2), activity(3)], "activity_pagination_overlap"),
        ([activity(1), activity(2)], "activity_pagination_no_progress"),
    ],
)
def test_partial_or_overlapping_pages_keep_previous_complete_current_data(
    settings, monkeypatch, second_page, failure_code
):
    req = only_activities(monkeypatch)
    collect(settings, END, gateway=SyntheticGateway([[activity(99)]]))
    with Repository(settings.data_dir) as repo:
        before = repo.list_records(resource="activities")
        original = repo.get_raw("activities", req.key)
    result = collect(
        settings,
        END,
        gateway=SyntheticGateway([[activity(1), activity(2)], second_page]),
    )
    assert result["status"] == "partial"
    with Repository(settings.data_dir) as repo:
        assert repo.list_records(resource="activities") == before
        assert repo.get_raw("activities", req.key) == original
        attempts = repo.list_requests(result["run_id"])
        assert attempts[0]["resource"] == "activities_page"
        assert attempts[-1]["resource"] == "activities"
        assert attempts[-1]["status"] == "failed"
        assert attempts[-1]["error_code"] == failure_code
        assert repo.get_raw("activities_page", req.key + ":offset=0") == [activity(1), activity(2)]


def test_retry_after_is_persisted_and_blocks_a_new_run_before_any_fetch(settings, monkeypatch):
    plan = [daily_request("sleep", END), daily_request("hrv", END)]
    monkeypatch.setattr("sleepscape.services.collection.build_plan", lambda *args: plan)
    gateway = SyntheticGateway([GarminError("retry_after_deferred", 429, retry_after_seconds=3600)])
    result = collect(settings, END, gateway=gateway)
    assert len(gateway.calls) == 1
    assert result["status"] == "partial"
    assert datetime.fromisoformat(result["retry_not_before"]) > datetime.now(UTC)
    with Repository(settings.data_dir) as repo:
        assert repo.latest_run()["summary"]["retry_not_before"] == result["retry_not_before"]
        assert repo.list_requests()[0]["http_status"] == 429
    retry_gateway = SyntheticGateway([])
    with pytest.raises(CollectionError, match="retry_after_wait_still_active"):
        collect(settings, END, gateway=retry_gateway)
    assert retry_gateway.calls == []
    with Repository(settings.data_dir) as repo:
        assert repo.counts()["runs"] == 1


@pytest.mark.parametrize("identity", [True, None, "not-an-integer-id"])
def test_invalid_activity_ids_never_create_current_records(settings, monkeypatch, identity):
    only_activities(monkeypatch)
    result = collect(settings, END, gateway=SyntheticGateway([[activity(identity)]]))
    assert result["status"] == "partial"
    with Repository(settings.data_dir) as repo:
        assert repo.list_records(resource="activities") == []
        assert repo.list_requests()[-1]["error_code"] == "activity_page_invalid_id"
