"""Read-only database boundary tests against disposable synthetic stores."""

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from sleepscape.domain.models import DataRequest, FetchResult, NormalizedBatch
from sleepscape.storage.reader import DashboardReader, StorageReadError, completed_run_plans
from sleepscape.storage.repository import Repository


def test_missing_database_has_empty_queries_and_creates_nothing(tmp_path):
    directory = tmp_path / "not_created"
    with DashboardReader(directory) as reader:
        assert not reader.present
        assert reader.summaries(date(2026, 1, 1), date(2026, 1, 2)) == []
        assert reader.attempts(date(2026, 1, 1), date(2026, 1, 2)) == []
        assert reader.status()["record_count"] == 0
    assert not directory.exists()
    assert completed_run_plans(directory) == []
    assert not directory.exists()


def test_sync_metadata_reader_selects_completed_plans_without_mutating_source(tmp_path):
    directory = tmp_path / "data"
    with Repository(directory) as repository:
        run = repository.start_run({"end_date": "2026-01-01", "history_days": 38})
        repository.finish_run(run, "completed", {})
        run = repository.start_run({"end_date": "2026-01-02", "history_days": 7})
        repository.finish_run(run, "failed", {})
    path = directory / "sleepscape.sqlite3"
    before = path.read_bytes()
    assert completed_run_plans(directory) == [{"end_date": "2026-01-01", "history_days": 38}]
    assert path.read_bytes() == before


def test_sync_metadata_reader_does_not_expose_corrupt_database_errors(tmp_path):
    (tmp_path / "sleepscape.sqlite3").write_bytes(b"synthetic-private-broken-database")
    with pytest.raises(StorageReadError, match="^run_metadata_unavailable$"):
        completed_run_plans(tmp_path)


def test_queries_use_sample_utc_not_source_partition_and_never_select_raw(tmp_path):
    directory = tmp_path / "data"
    with Repository(directory) as repository:
        run = repository.start_run({})
        request = DataRequest(
            "hrv", "synthetic", "/synthetic", start_date="2026-01-02", end_date="2026-01-02"
        )
        records = [
            {
                "kind": "hrv_sample",
                "key": "one",
                "date": "2026-01-02",
                "timestamp": "2026-01-01T23:59:00Z",
                "data": {"hrv_ms": 30},
            },
            {
                "kind": "hrv_sample",
                "key": "two",
                "date": "2026-01-02",
                "timestamp": "2026-01-02T08:01:00+08:00",
                "data": {"hrv_ms": 40},
            },
            {
                "kind": "hrv_sample",
                "key": "outside",
                "date": "2026-01-02",
                "timestamp": "2026-01-02T20:00:00Z",
                "data": {"hrv_ms": 50},
            },
        ]
        repository.save_snapshot(
            run,
            FetchResult(request, {"private": "never select"}, "2026-01-03T00:00:00Z", 200),
            NormalizedBatch(records),
        )
    before = (directory / "sleepscape.sqlite3").read_bytes()
    start = datetime(2026, 1, 1, 23, 58, tzinfo=UTC)
    with DashboardReader(directory) as reader:
        reads = []
        reader.connection.set_trace_callback(reads.append)
        samples = reader.samples(start, start + timedelta(minutes=5))
        assert [row["key"] for row in samples] == ["one", "two"]
        assert all(row["date"] == "2026-01-02" for row in samples)
        assert reader.summaries(date(2026, 1, 1), date(2026, 1, 2)) == []
        assert len(reader.attempts(date(2026, 1, 2), date(2026, 1, 2))) == 1
        assert not any("raw_snapshots" in query for query in reads)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.connection.execute("DELETE FROM normalized_records")
    assert (directory / "sleepscape.sqlite3").read_bytes() == before


def test_query_ranges_are_bounded_even_without_database(tmp_path):
    with DashboardReader(tmp_path / "missing") as reader:
        with pytest.raises(ValueError):
            reader.summaries(date(2026, 1, 1), date(2027, 1, 1))
        with pytest.raises(ValueError):
            reader.available_dates(date(2026, 1, 1), date(2026, 4, 3))
        first = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError):
            reader.samples(first, first + timedelta(hours=49))
        with pytest.raises(ValueError):
            reader.events(first.replace(tzinfo=None), first + timedelta(hours=1))


def test_read_transaction_sees_consistent_snapshot_while_writer_updates(tmp_path):
    directory = tmp_path / "data"
    with Repository(directory) as repository:
        run = repository.start_run({})
        request = DataRequest(
            "sleep", "day", "/synthetic", start_date="2026-01-01", end_date="2026-01-01"
        )

        def save(value):
            repository.save_snapshot(
                run,
                FetchResult(request, value, "2026-01-02T00:00:00Z", 200),
                NormalizedBatch(
                    [
                        {
                            "kind": "sleep",
                            "key": "day",
                            "date": "2026-01-01",
                            "timestamp": None,
                            "data": {"sleep_seconds": value},
                        }
                    ]
                ),
            )

        save(10)
        with DashboardReader(directory) as reader:
            assert (
                reader.summaries(date(2026, 1, 1), date(2026, 1, 1))[0]["data"]["sleep_seconds"]
                == 10
            )
            save(20)
            assert (
                reader.summaries(date(2026, 1, 1), date(2026, 1, 1))[0]["data"]["sleep_seconds"]
                == 10
            )
        with DashboardReader(directory) as reader:
            assert (
                reader.summaries(date(2026, 1, 1), date(2026, 1, 1))[0]["data"]["sleep_seconds"]
                == 20
            )
