from __future__ import annotations

import sqlite3

import pytest

from smartstitch.database import SQLiteStore


def test_sqlite_store_closes_every_operation_connection(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "smartstitch.db")
    opened: list[sqlite3.Connection] = []
    original_connect = store.connect

    def tracked_connect() -> sqlite3.Connection:
        connection = original_connect()
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, "connect", tracked_connect)
    store.ensure_job_table("jobs")
    job = {
        "id": "job-1",
        "status": "completed",
        "finished_at": "2026-09-18T00:00:00+08:00",
    }

    for _ in range(100):
        store.save("jobs", job)
        assert store.get("jobs", "job-1") == job
        assert store.list("jobs") == [job]

    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize(
    ("table", "request_key"),
    [("jobs", False), ("slice_jobs", True)],
)
def test_sqlite_store_keeps_only_latest_100_records(
    tmp_path, table, request_key
):
    store = SQLiteStore(tmp_path / "smartstitch.db")
    store.ensure_job_table(table, request_key=request_key)

    for index in range(105):
        job = {"id": f"job-{index:03d}", "status": "completed"}
        if request_key:
            job["request_key"] = f"request-{index:03d}"
        store.save(table, job)

    records = store.list(table)
    assert len(records) == 100
    assert {record["id"] for record in records} == {
        f"job-{index:03d}" for index in range(5, 105)
    }


def test_sqlite_store_preserves_active_records_when_pruning(tmp_path):
    store = SQLiteStore(tmp_path / "smartstitch.db")
    store.ensure_job_table("jobs")

    store.save("jobs", {"id": "active", "status": "running"})
    for index in range(100):
        store.save(
            "jobs",
            {"id": f"finished-{index:03d}", "status": "completed"},
        )

    records = store.list("jobs")
    assert len(records) == 100
    assert store.get("jobs", "active") is not None
    assert store.get("jobs", "finished-000") is None


def test_sqlite_store_prunes_existing_records_when_table_is_initialized(tmp_path):
    path = tmp_path / "smartstitch.db"
    store = SQLiteStore(path)
    store.ensure_job_table("jobs")
    with store.connection() as connection:
        connection.executemany(
            "INSERT INTO jobs(id, payload, updated_at) VALUES(?, ?, ?)",
            (
                (
                    f"legacy-{index:03d}",
                    '{"status": "completed"}',
                    f"2026-09-18T00:00:{index:03d}+08:00",
                )
                for index in range(105)
            ),
        )

    store.ensure_job_table("jobs")

    assert len(store.list("jobs")) == 100
    assert store.get("jobs", "legacy-000") is None
