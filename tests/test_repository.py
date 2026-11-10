"""Synthetic persistence tests; no personal data, credentials, or network."""

import sqlite3
import stat

import pytest

from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.storage import Repository


def request(key="window", start="2026-01-01", end="2026-01-03", resource="activities"):
    return DataRequest(resource, key, "/synthetic", start_date=start, end_date=end)


def record(key="a", date="2026-01-02", value=1):
    return {"kind": "activity", "key": key, "date": date, "timestamp": None, "data": {"x": value}}


def save(repo, run_id, req=None, records=None, payload=None, at="2026-01-04T00:00:00Z"):
    records = [record()] if records is None else records
    return repo.save_snapshot(
        run_id,
        FetchResult(req or request(), records if payload is None else payload, at, 200),
        NormalizedBatch(records, metrics={"payload_shape_valid": True}),
    )


def test_same_payload_is_deduplicated_but_latest_fetch_is_visible(tmp_path):
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run({"source": "synthetic"})
        first = save(repo, run_id, payload={"b": 2, "a": 1})
        second = save(repo, run_id, payload={"a": 1, "b": 2}, at="2026-01-05T00:00:00Z")
        assert first["new_snapshot"] is True
        assert second["new_snapshot"] is False
        assert first["snapshot_id"] == second["snapshot_id"]
        assert repo.counts() == {
            "runs": 1,
            "raw_snapshots": 1,
            "normalized_records": 1,
            "requests": 2,
        }
        assert repo.list_records()[0]["fetched_at"] == "2026-01-05T00:00:00Z"
        assert repo.get_raw("activities", "window") == {"a": 1, "b": 2}
        assert "payload" not in repo.list_requests()[0]
        repo.finish_run(run_id, "ok", {"requests": 2})
        assert repo.integrity_check() == ["ok"]


def test_revision_keeps_raw_history_and_replaces_or_deletes_current_rows(tmp_path):
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run({})
        first = save(repo, run_id, records=[record("a"), record("b")])
        second = save(repo, run_id, records=[record("a", value=2)], at="2026-01-05T00:00:00Z")
        assert second["snapshot_id"] != first["snapshot_id"]
        assert repo.counts()["raw_snapshots"] == 2
        assert [(r["key"], r["data"]) for r in repo.list_records()] == [("a", {"x": 2})]
        assert repo.get_raw("activities", "window") == [record("a", value=2)]


def test_overlapping_ranges_share_one_record_identity_and_preserve_outside_scope(tmp_path):
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run({})
        save(repo, run_id, records=[record("old", "2026-01-01"), record("shared")])
        save(
            repo,
            run_id,
            request("shifted", "2026-01-02", "2026-01-04"),
            [record("shared", value=3), record("new", "2026-01-04")],
        )
        rows = repo.list_records(resource="activities")
        assert [r["key"] for r in rows] == ["old", "shared", "new"]
        assert rows[1]["request_key"] == "shifted"
        assert rows[1]["data"] == {"x": 3}
        assert len(repo.list_records(start_date="2026-01-02", end_date="2026-01-03")) == 1
        # The same stable key moving outside the new window still updates one global identity.
        save(
            repo,
            run_id,
            request("later", "2026-01-05", "2026-01-05"),
            [record("shared", "2026-01-05")],
        )
        assert len(repo.list_records()) == 3
        assert len(repo.list_records(kind="missing")) == 0


def test_failure_and_invalid_response_preserve_previous_data_and_valid_raw(tmp_path):
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run({})
        save(repo, run_id)
        before = repo.list_records()
        repo.record_failure(run_id, request(), "upstream_unavailable", 503)
        invalid = repo.save_snapshot(
            run_id,
            FetchResult(request(), {"unexpected": True}, "2026-01-05T00:00:00Z", 200),
            NormalizedBatch(
                issues=[{"severity": "error", "code": "shape", "field": "root", "raw": "omit"}],
                metrics={"payload_shape_valid": False},
            ),
        )
        assert invalid["status"] == "invalid"
        assert repo.list_records() == before
        assert repo.get_raw("activities", "window") == [record()]
        requests = repo.list_requests(run_id)
        assert [r["status"] for r in requests] == ["ok", "failed", "invalid"]
        assert requests[-1]["issues"] == [{"severity": "error", "code": "shape", "field": "root"}]
        assert requests[1]["http_status"] == 503
        assert repo.get_raw("unknown", "unknown") is None


def test_valid_empty_clears_only_its_scope_and_pages_are_isolated(tmp_path):
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run({})
        save(repo, run_id, records=[record("a"), record("outside", "2026-01-01")])
        save(repo, run_id, request(resource="activities_page"), [], {"page": []})
        assert len(repo.list_records()) == 2
        result = save(repo, run_id, request("empty", "2026-01-02", "2026-01-03"), [])
        assert result["status"] == "empty"
        assert [r["key"] for r in repo.list_records()] == ["outside"]
        assert repo.get_raw("activities", "empty") == []


def test_undated_rows_are_scoped_by_request_key(tmp_path):
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run({})
        save(repo, run_id, request("first"), [record("first", None)])
        save(repo, run_id, request("second"), [record("second", None)])
        save(repo, run_id, request("first"), [])
        assert [r["key"] for r in repo.list_records()] == ["second"]


def test_transaction_rolls_back_snapshot_and_replacement_on_bad_record(tmp_path):
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run({})
        save(repo, run_id)
        before, counts = repo.list_records(), repo.counts()
        with pytest.raises(KeyError):
            save(repo, run_id, records=[record("valid"), {"kind": "activity"}])
        assert repo.list_records() == before
        assert repo.counts() == counts
        with pytest.raises(sqlite3.IntegrityError):
            save(repo, "unknown-run", records=[record(value=9)])
        assert repo.list_records() == before
        assert repo.counts() == counts
        assert repo.integrity_check() == ["ok"]


def test_permissions_wal_and_reopening(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir(mode=0o755)
    with Repository(directory) as repo:
        run_id = repo.start_run({})
        save(repo, run_id)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        paths = list(directory.glob("sleepscape.sqlite3*"))
        assert any(path.name.endswith("-wal") for path in paths)
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    with Repository(directory) as repo:
        assert len(repo.list_records()) == 1
        assert repo.integrity_check() == ["ok"]


def test_latest_run_returns_decoded_plan_and_summary_even_for_unfinished_run(tmp_path, monkeypatch):
    monkeypatch.setattr("sleepscape.storage.repository._now", lambda: "2026-01-04T00:00:00Z")
    with Repository(tmp_path / "data") as repo:
        assert repo.latest_run() is None
        first = repo.start_run({"start_date": "2026-01-01"})
        repo.finish_run(first, "ok", {"requests": 1})
        assert repo.latest_run()["summary"] == {"requests": 1}
        second = repo.start_run({"start_date": "2026-01-02"})
        assert repo.latest_run() == {
            "id": second,
            "status": "running",
            "started_at": "2026-01-04T00:00:00Z",
            "finished_at": None,
            "plan": {"start_date": "2026-01-02"},
            "summary": None,
        }


@pytest.mark.parametrize(("items", "status"), [([{"synthetic": True}], "ok"), ([], "empty")])
def test_raw_only_activity_page_reports_payload_presence(tmp_path, items, status):
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run({})
        result = repo.save_snapshot(
            run_id,
            FetchResult(request(resource="activities_page"), items, "2026-01-04T00:00:00Z", 200),
            NormalizedBatch(metrics={"raw_only": True, "item_count": len(items)}),
        )
        assert result["status"] == status
        assert result["record_count"] == 0
        assert repo.list_records() == []
        assert repo.get_raw("activities_page", "window") == items


def test_cross_midnight_samples_use_source_partition_and_revisions_clear_old_assignment(tmp_path):
    first = request("2026-01-02", "2026-01-02", "2026-01-02", "hrv")
    second = request("2026-01-03", "2026-01-03", "2026-01-03", "hrv")
    first_sample = {
        "kind": "hrv_sample",
        "key": "2026-01-01T15:00:00Z",
        "date": "2026-01-02",
        "timestamp": "2026-01-01T15:00:00Z",
        "data": {"hrv_ms": 50},
    }
    second_sample = {
        "kind": "hrv_sample",
        "key": "2026-01-02T15:00:00Z",
        "date": "2026-01-03",
        "timestamp": "2026-01-02T15:00:00Z",
        "data": {"hrv_ms": 55},
    }
    with Repository(tmp_path / "data") as repo:
        run_id = repo.start_run({})
        # An earlier normalization assigned the first sample to its measurement day.
        save(repo, run_id, first, [{**first_sample, "date": "2026-01-01", "key": "obsolete"}])
        save(repo, run_id, second, [second_sample])
        save(repo, run_id, first, [first_sample])
        assert [r["key"] for r in repo.list_records()] == [
            first_sample["key"],
            second_sample["key"],
        ]
        # Clearing Jan 2's sleep must not erase Jan 3's sleep samples measured on Jan 2.
        save(repo, run_id, first, [])
        assert [r["key"] for r in repo.list_records()] == [second_sample["key"]]
