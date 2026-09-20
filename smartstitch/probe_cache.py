from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .database import SQLiteStore
from .models import MediaProbe


PROBE_SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class ProbeFingerprint:
    path: Path
    size_bytes: int
    modified_at_ns: int
    profile: str

    @property
    def key(self) -> tuple[str, str]:
        return str(self.path), self.profile


@dataclass(frozen=True)
class CachedProbe:
    probe: MediaProbe | None = None
    error: str | None = None


class MediaProbeCache:
    """Persistent ffprobe result cache stored in SmartStitch's runtime database."""

    def __init__(self, store: SQLiteStore):
        self.store = store
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self.store.lock, self.store.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS media_probe_cache ("
                "path TEXT NOT NULL, "
                "probe_profile TEXT NOT NULL, "
                "size_bytes INTEGER NOT NULL, "
                "modified_at_ns INTEGER NOT NULL, "
                "probe_schema_version INTEGER NOT NULL, "
                "ffprobe_signature TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "probe_json TEXT, "
                "error_text TEXT, "
                "checked_at TEXT NOT NULL, "
                "failure_expires_at TEXT, "
                "last_used_at TEXT NOT NULL, "
                "PRIMARY KEY(path, probe_profile))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS media_probe_cache_last_used_idx "
                "ON media_probe_cache(last_used_at)"
            )

    def get_many(
        self,
        fingerprints: Iterable[ProbeFingerprint],
        *,
        ffprobe_signature: str,
    ) -> dict[tuple[str, str], CachedProbe]:
        requested = {fingerprint.key: fingerprint for fingerprint in fingerprints}
        if not requested:
            return {}

        now = _now()
        hits: dict[tuple[str, str], CachedProbe] = {}
        stale: list[tuple[str, str]] = []
        with self.store.lock, self.store.connection() as connection:
            for key, fingerprint in requested.items():
                row = connection.execute(
                    "SELECT size_bytes, modified_at_ns, probe_schema_version, "
                    "ffprobe_signature, status, probe_json, error_text, "
                    "failure_expires_at FROM media_probe_cache "
                    "WHERE path = ? AND probe_profile = ?",
                    key,
                ).fetchone()
                if row is None:
                    continue
                if (
                    row[0] != fingerprint.size_bytes
                    or row[1] != fingerprint.modified_at_ns
                    or row[2] != PROBE_SCHEMA_VERSION
                    or row[3] != ffprobe_signature
                ):
                    stale.append(key)
                    continue

                status, probe_json, error_text, failure_expires_at = row[4:]
                try:
                    if status == "passed" and probe_json:
                        hits[key] = CachedProbe(
                            probe=MediaProbe.model_validate(json.loads(probe_json))
                        )
                    elif (
                        status == "failed"
                        and error_text
                        and failure_expires_at
                        and datetime.fromisoformat(failure_expires_at) > now
                    ):
                        hits[key] = CachedProbe(error=error_text)
                    else:
                        stale.append(key)
                except (ValueError, TypeError, json.JSONDecodeError):
                    stale.append(key)

            if hits:
                timestamp = now.isoformat(timespec="seconds")
                connection.executemany(
                    "UPDATE media_probe_cache SET last_used_at = ? "
                    "WHERE path = ? AND probe_profile = ?",
                    ((timestamp, path, profile) for path, profile in hits),
                )
            if stale:
                connection.executemany(
                    "DELETE FROM media_probe_cache "
                    "WHERE path = ? AND probe_profile = ?",
                    stale,
                )
        return hits

    def put_many(
        self,
        entries: Iterable[tuple[ProbeFingerprint, CachedProbe]],
        *,
        ffprobe_signature: str,
        failure_ttl_seconds: int,
    ) -> None:
        now = _now()
        checked_at = now.isoformat(timespec="seconds")
        values = []
        for fingerprint, result in entries:
            failed = result.probe is None
            expires_at = (
                (now + timedelta(seconds=failure_ttl_seconds)).isoformat(
                    timespec="seconds"
                )
                if failed
                else None
            )
            values.append(
                (
                    str(fingerprint.path),
                    fingerprint.profile,
                    fingerprint.size_bytes,
                    fingerprint.modified_at_ns,
                    PROBE_SCHEMA_VERSION,
                    ffprobe_signature,
                    "failed" if failed else "passed",
                    (
                        result.probe.model_dump_json()
                        if result.probe is not None
                        else None
                    ),
                    result.error,
                    checked_at,
                    expires_at,
                    checked_at,
                )
            )
        if not values:
            return
        with self.store.lock, self.store.connection() as connection:
            connection.executemany(
                "INSERT INTO media_probe_cache("
                "path, probe_profile, size_bytes, modified_at_ns, "
                "probe_schema_version, ffprobe_signature, status, probe_json, "
                "error_text, checked_at, failure_expires_at, last_used_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path, probe_profile) DO UPDATE SET "
                "size_bytes=excluded.size_bytes, "
                "modified_at_ns=excluded.modified_at_ns, "
                "probe_schema_version=excluded.probe_schema_version, "
                "ffprobe_signature=excluded.ffprobe_signature, "
                "status=excluded.status, probe_json=excluded.probe_json, "
                "error_text=excluded.error_text, checked_at=excluded.checked_at, "
                "failure_expires_at=excluded.failure_expires_at, "
                "last_used_at=excluded.last_used_at",
                values,
            )

    def prune(self, *, max_age_days: int = 30, max_records: int = 100_000) -> None:
        cutoff = (_now() - timedelta(days=max_age_days)).isoformat(timespec="seconds")
        with self.store.lock, self.store.connection() as connection:
            connection.execute(
                "DELETE FROM media_probe_cache WHERE last_used_at < ?", (cutoff,)
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM media_probe_cache"
            ).fetchone()[0]
            excess = count - max_records
            if excess > 0:
                connection.execute(
                    "DELETE FROM media_probe_cache WHERE rowid IN ("
                    "SELECT rowid FROM media_probe_cache "
                    "ORDER BY last_used_at ASC LIMIT ?)",
                    (excess,),
                )
