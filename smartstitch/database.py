from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SQLiteStore:
    """Shared SQLite access and write coordination for all task tables."""

    ALLOWED_TABLES = {"jobs", "slice_jobs", "output_sync_jobs", "upscale_jobs", "upscale_batches"}
    MAX_RECORDS_PER_TABLE = 100
    ACTIVE_STATUSES = {"queued", "running", "cancelling"}

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.execute("PRAGMA busy_timeout=20000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open one transaction and always release its file descriptors."""
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def ensure_job_table(self, table: str, *, request_key: bool = False) -> None:
        self._validate_table(table)
        request_column = ", request_key TEXT NOT NULL UNIQUE" if request_key else ""
        with self.lock, self.connection() as connection:
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                f"id TEXT PRIMARY KEY{request_column}, "
                "payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            self._prune_with_connection(connection, table)

    def mark_active_interrupted(self, table: str) -> None:
        self._validate_table(table)
        with self.lock, self.connection() as connection:
            rows = connection.execute(f"SELECT id, payload FROM {table}").fetchall()
            for record_id, payload in rows:
                data = json.loads(payload)
                if data.get("status") not in {"queued", "running", "cancelling"}:
                    continue
                data["status"] = "interrupted"
                data["finished_at"] = _now()
                self._save_with_connection(connection, table, data)
            self._prune_with_connection(connection, table)

    def save(self, table: str, value: dict[str, Any]) -> None:
        self._validate_table(table)
        with self.lock, self.connection() as connection:
            self._save_with_connection(connection, table, value)
            self._prune_with_connection(connection, table)

    def get(self, table: str, record_id: str) -> dict[str, Any] | None:
        self._validate_table(table)
        with self.lock, self.connection() as connection:
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE id = ?", (record_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get_by_request_key(
        self, table: str, request_key: str
    ) -> dict[str, Any] | None:
        self._validate_table(table)
        with self.lock, self.connection() as connection:
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE request_key = ?", (request_key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, table: str) -> list[dict[str, Any]]:
        self._validate_table(table)
        with self.lock, self.connection() as connection:
            rows = connection.execute(
                f"SELECT payload FROM {table} "
                "ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, table: str, record_id: str) -> bool:
        self._validate_table(table)
        with self.lock, self.connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE id = ?", (record_id,)
            )
        return cursor.rowcount > 0

    def delete_all(self, table: str) -> int:
        self._validate_table(table)
        with self.lock, self.connection() as connection:
            cursor = connection.execute(f"DELETE FROM {table}")
        return cursor.rowcount

    def _save_with_connection(
        self,
        connection: sqlite3.Connection,
        table: str,
        value: dict[str, Any],
    ) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        if table == "slice_jobs":
            connection.execute(
                "INSERT INTO slice_jobs(id, request_key, payload, updated_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "request_key=excluded.request_key, payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (value["id"], value["request_key"], payload, _now()),
            )
            return
        connection.execute(
            f"INSERT INTO {table}(id, payload, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (value["id"], payload, _now()),
        )

    def _prune_with_connection(
        self,
        connection: sqlite3.Connection,
        table: str,
    ) -> None:
        rows = connection.execute(
            f"SELECT id, payload FROM {table} "
            "ORDER BY updated_at DESC, rowid DESC"
        ).fetchall()
        excess = len(rows) - self.MAX_RECORDS_PER_TABLE
        if excess <= 0:
            return

        removable: list[str] = []
        for record_id, payload in reversed(rows):
            try:
                status = json.loads(payload).get("status")
            except (json.JSONDecodeError, AttributeError):
                status = None
            if status not in self.ACTIVE_STATUSES:
                removable.append(record_id)
                if len(removable) == excess:
                    break

        connection.executemany(
            f"DELETE FROM {table} WHERE id = ?",
            ((record_id,) for record_id in removable),
        )

    def _validate_table(self, table: str) -> None:
        if table not in self.ALLOWED_TABLES:
            raise ValueError(f"Unsupported task table: {table}")
