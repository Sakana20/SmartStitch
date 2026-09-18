from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from smartstitch.api import create_app
from smartstitch.models import CreateLibraryRequest


def setup_slice_app(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    app.state.library_service.create(
        CreateLibraryRequest(
            new_id="slice-library",
            new_name="切片库",
            parent_directory=str(tmp_path),
            folder_name="library",
            client_request_id="library-request-1",
        )
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_stat = source.stat()
    review_revision = "b" * 64

    def write_analysis(analysis_id: str) -> None:
        record = {
            "analysis_id": analysis_id,
            "source_path": str(source),
            "source_fingerprint": {
                "size_bytes": source_stat.st_size,
                "modified_at_ns": source_stat.st_mtime_ns,
            },
            "fps": 25,
            "frame_count": 50,
            "review": {
                "review_revision": review_revision,
                "segments": [
                    {"index": 1, "start_frame": 0, "end_frame": 25},
                    {"index": 2, "start_frame": 25, "end_frame": 50},
                ],
            },
        }
        path = app.state.timeline_analyzer.data_directory / f"{analysis_id}.json"
        path.write_text(json.dumps(record), encoding="utf-8")

    write_analysis("a" * 24)
    write_analysis("c" * 24)
    config_hash = app.state.config_store.content_hash("slice-library")
    return app, review_revision, config_hash


def request_payload(
    analysis_id: str,
    review_revision: str,
    config_hash: str,
    request_id: str,
):
    return {
        "analysis_id": analysis_id,
        "config_id": "slice-library",
        "review_revision": review_revision,
        "current_config_hash": config_hash,
        "assignments": [{"segment_index": 1, "category": "hook"}],
        "client_request_id": request_id,
    }


def wait_for_status(client: TestClient, job_id: str, expected: set[str]):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/timeline/slice-jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in expected:
            return job
        time.sleep(0.02)
    raise AssertionError(f"slice job {job_id} did not reach {expected}")


def test_async_slice_job_persists_and_is_idempotent(tmp_path, monkeypatch):
    app, review_revision, config_hash = setup_slice_app(tmp_path)

    def runner(command, **_kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [{"codec_type": "video"}]}),
            stderr="",
        )

    monkeypatch.setattr(app.state.timeline_slicer, "runner", runner)
    payload = request_payload(
        "a" * 24, review_revision, config_hash, "async-slice-request-1"
    )
    with TestClient(app) as client:
        created = client.post("/api/v1/timeline/slice-jobs", json=payload)
        assert created.status_code == 202
        job_id = created.json()["id"]

        repeated = client.post("/api/v1/timeline/slice-jobs", json=payload)
        assert repeated.status_code == 202
        assert repeated.json()["id"] == job_id
        assert repeated.json()["idempotent"] is True

        finished = wait_for_status(client, job_id, {"completed"})
        assert finished["success_count"] == 1
        assert finished["progress"] == 1.0
        assert Path(finished["manifest_path"]).is_file()
        assert client.get("/api/v1/timeline/slice-jobs").json()[0]["id"] == job_id

        deleted = client.delete(f"/api/v1/timeline/slice-jobs/{job_id}")
        assert deleted.status_code == 200
        restored = client.post("/api/v1/timeline/slice-jobs", json=payload)
        assert restored.status_code == 202
        assert restored.json()["id"] == job_id
        assert restored.json()["idempotent"] is True

    with sqlite3.connect(tmp_path / "data" / "smartstitch.db") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"jobs", "slice_jobs"}.issubset(tables)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM slice_jobs").fetchone()[0] == 1


def test_queued_slice_job_can_be_cancelled_while_previous_job_runs(tmp_path, monkeypatch):
    app, review_revision, config_hash = setup_slice_app(tmp_path)
    first_started = threading.Event()
    release_first = threading.Event()

    def runner(command, **_kwargs):
        if command[0] == "ffmpeg":
            first_started.set()
            assert release_first.wait(timeout=5)
            Path(command[-1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [{"codec_type": "video"}]}),
            stderr="",
        )

    monkeypatch.setattr(app.state.timeline_slicer, "runner", runner)
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/timeline/slice-jobs",
            json=request_payload(
                "a" * 24, review_revision, config_hash, "async-slice-first"
            ),
        ).json()
        assert first_started.wait(timeout=2)

        second_response = client.post(
            "/api/v1/timeline/slice-jobs",
            json=request_payload(
                "c" * 24, review_revision, config_hash, "async-slice-second"
            ),
        )
        assert second_response.status_code == 202
        second = second_response.json()
        assert second["status"] == "queued"

        cancelled = client.post(
            f"/api/v1/timeline/slice-jobs/{second['id']}/cancel"
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        release_first.set()
        wait_for_status(client, first["id"], {"completed"})
        saved_second = client.get(
            f"/api/v1/timeline/slice-jobs/{second['id']}"
        ).json()
        assert saved_second["status"] == "cancelled"
        assert saved_second["cancelled_count"] == 1


def test_failed_slice_job_can_retry_from_persisted_snapshot(tmp_path, monkeypatch):
    app, review_revision, config_hash = setup_slice_app(tmp_path)
    ffmpeg_calls = 0

    def runner(command, **_kwargs):
        nonlocal ffmpeg_calls
        if command[0] == "ffmpeg":
            ffmpeg_calls += 1
            if ffmpeg_calls == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="broken")
            Path(command[-1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [{"codec_type": "video"}]}),
            stderr="",
        )

    monkeypatch.setattr(app.state.timeline_slicer, "runner", runner)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/timeline/slice-jobs",
            json=request_payload(
                "a" * 24, review_revision, config_hash, "async-slice-retry"
            ),
        ).json()
        failed = wait_for_status(client, created["id"], {"failed"})
        assert failed["items"][0]["attempts"] == 1

        retried = client.post(
            f"/api/v1/timeline/slice-jobs/{created['id']}/retry-failed"
        )
        assert retried.status_code == 200
        assert retried.json()["status"] == "queued"
        completed = wait_for_status(client, created["id"], {"completed"})
        assert completed["items"][0]["attempts"] == 2
        assert completed["success_count"] == 1


def test_unexpected_worker_error_does_not_leave_slice_job_running(
    tmp_path, monkeypatch
):
    app, review_revision, config_hash = setup_slice_app(tmp_path)

    def fail_execute(job, **_kwargs):
        job["status"] = "running"
        job["items"][0]["status"] = "running"
        job["items"][0]["phase"] = "encoding"
        raise RuntimeError("state persistence failed")

    monkeypatch.setattr(app.state.timeline_slicer, "execute", fail_execute)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/timeline/slice-jobs",
            json=request_payload(
                "a" * 24,
                review_revision,
                config_hash,
                "async-slice-worker-error",
            ),
        ).json()

        failed = wait_for_status(client, created["id"], {"failed"})
        assert failed["error"] == "state persistence failed"
        assert failed["items"][0]["status"] == "failed"
        assert failed["items"][0]["phase"] == "done"


def test_background_worker_requests_low_process_priority(tmp_path, monkeypatch):
    app, review_revision, config_hash = setup_slice_app(tmp_path)
    execution_started = threading.Event()
    received_options = {}

    def execute(job, **kwargs):
        received_options.update(kwargs)
        job["status"] = "completed"
        job["ok"] = True
        job["progress"] = 1.0
        kwargs["on_update"](job)
        execution_started.set()
        return job

    monkeypatch.setattr(app.state.timeline_slicer, "execute", execute)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/timeline/slice-jobs",
            json=request_payload(
                "a" * 24,
                review_revision,
                config_hash,
                "async-slice-low-priority",
            ),
        )
        assert response.status_code == 202
        assert execution_started.wait(timeout=2)

    assert received_options["low_priority"] is True
