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
