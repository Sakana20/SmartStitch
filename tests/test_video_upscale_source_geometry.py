from __future__ import annotations

from conftest import authenticated_client

import subprocess
import time
from pathlib import Path

import pytest

import smartstitch.video_upscale as upscale
from smartstitch.api import create_app
from smartstitch.database import SQLiteStore


def test_model_status_api_uses_selected_model(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    try:
        client = authenticated_client(app)
        selected = client.get("/api/v1/tools/video-upscale/model?model=animevideo")
        assert selected.status_code == 200
        assert selected.json()["model"] == "animevideo"
        assert selected.json()["scale"] == 4
        assert client.get("/api/v1/tools/video-upscale/model?model=unknown").status_code == 422
    finally:
        app.state.instance_lock.release()


@pytest.mark.parametrize("model_name", ["x2plus", "animevideo"])
def test_bundled_model_is_ready_without_network_or_writable_install(tmp_path, monkeypatch, model_name):
    resources = tmp_path / "app-resources"
    name = upscale.model_asset_name(model_name)
    package = resources / "models" / f"{name}.mlpackage"
    (package / "Data/com.apple.CoreML/weights").mkdir(parents=True)
    for relative in ("Manifest.json", "Data/com.apple.CoreML/model.mlmodel",
                     "Data/com.apple.CoreML/weights/weight.bin"):
        (package / relative).write_bytes(b"test")
    (package.parent / f"{name}.sha256").write_text(upscale.MODEL_CONFIGS[model_name]["sha256"])
    monkeypatch.setattr(upscale, "resource_root", lambda: resources)
    monkeypatch.setattr(upscale.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(upscale.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(upscale, "urlopen", lambda *_args, **_kwargs: pytest.fail("bundled model tried to download"))

    assert upscale.model_directory(tmp_path / "writable-data", model_name) == package
    status = upscale.prepare_model(tmp_path / "writable-data", model_name=model_name)
    assert status["available"] and status["source"] == "bundled"
    assert not (tmp_path / "writable-data").exists()

    (package.parent / f"{name}.sha256").write_text("invalid")
    assert upscale.model_status(tmp_path / "writable-data", model_name)["source"] == "missing"


def test_cluster_mode_scans_fixed_nas_folders(tmp_path):
    shared = tmp_path / "nas" / "Smartstitch"
    (shared / "超分" / "原素材").mkdir(parents=True)
    (shared / "超分" / "已处理").mkdir()
    app = create_app(base_directory=tmp_path, config_directory=shared,
                     data_directory=tmp_path / "data")
    try:
        client = authenticated_client(app)
        preview = client.get("/api/v1/tools/video-upscale/nas?model=x2plus")
        assert preview.status_code == 200
        assert preview.json()["source_directory"] == str(shared / "超分" / "原素材")
        assert preview.json()["output_directory"] == str(shared / "超分" / "已处理")
        assert preview.json()["pending_count"] == 0
        submitted = client.post("/api/v1/tools/video-upscale", json={"mode": "cluster", "model": "x2plus"})
        assert submitted.status_code == 422
        assert "没有待处理视频" in submitted.json()["detail"]
    finally:
        app.state.instance_lock.release()


def test_local_mode_previews_a_directory(tmp_path):
    (tmp_path / "config").mkdir()
    source = tmp_path / "videos"
    source.mkdir()
    app = create_app(tmp_path)
    try:
        client = authenticated_client(app)
        response = client.post("/api/v1/tools/video-upscale/local-preview", json={"source": str(source), "model": "x2plus"})
        assert response.status_code == 200
        assert response.json()["pending_count"] == 0
        assert response.json()["output_directory"] == str(source / "已处理")
        submitted = client.post("/api/v1/tools/video-upscale", json={"source": str(source), "mode": "local"})
        assert submitted.status_code == 422
        assert "没有待处理视频" in submitted.json()["detail"]
    finally:
        app.state.instance_lock.release()


def test_upscale_jobs_join_task_records_without_duplicate_batch_children(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    try:
        store = app.state.database_store
        store.save("upscale_local_jobs", {"id": "local-1", "status": "completed", "source": "/tmp/local.mp4",
                                         "model": "x2plus", "processed_frames": 10, "total_frames": 10,
                                         "created_at": "2026-09-24T01:00:00+00:00"})
        store.save("upscale_batches", {"id": "batch-1", "status": "running", "kind": "batch", "mode": "cluster",
                                      "model": "animevideo", "items": [{"job_id": "child-1"}],
                                      "processed_frames": 4, "total_frames": 20,
                                      "created_at": 1790215200.0})
        store.save("upscale_jobs", {"id": "child-1", "status": "running", "source": "/tmp/nas.mp4",
                                   "created_at": 1790215201.0})
        client = authenticated_client(app)
        jobs = client.get("/api/v1/tools/video-upscale/jobs")
        assert jobs.status_code == 200
        assert {job["id"] for job in jobs.json()} == {"local-1", "batch-1"}
        assert all("items" not in job and "segments" not in job for job in jobs.json())
        assert client.get("/api/v1/tools/video-upscale/local-1").json()["status"] == "completed"
        assert client.delete("/api/v1/tools/video-upscale/batch-1").status_code == 409
        assert client.delete("/api/v1/tools/video-upscale/local-1").status_code == 200
        assert {job["id"] for job in client.get("/api/v1/tools/video-upscale/jobs").json()} == {"batch-1"}
    finally:
        app.state.instance_lock.release()


@pytest.mark.parametrize("width,height,fps", [(160, 90, 10), (120, 200, 12)])
def test_output_geometry_and_frame_rate_follow_each_source(tmp_path, monkeypatch, width, height, fps):
    source = tmp_path / f"input-{width}x{height}-{fps}.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"color=c=blue:s={width}x{height}:r={fps}:d={3/fps}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={3/fps}",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True)
    monkeypatch.setattr(upscale, "model_status", lambda _directory, _model="x2plus": {"available": True})
    monkeypatch.setattr(upscale, "_load_model", lambda _directory, _model="x2plus": (None, "input", "output"))
    monkeypatch.setattr(upscale, "_upscale_frame", lambda frame, *_args: frame.tobytes())

    database = SQLiteStore(tmp_path / "upscale-jobs.db")
    manager = upscale.VideoUpscaleManager(tmp_path, database)
    job = manager.create(str(source), str(tmp_path))
    assert (job["width"], job["height"], job["fps"]) == (width, height, f"{fps}/1")
    deadline = time.monotonic() + 10
    while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.05)
        job = manager.get(job["id"])

    assert job["status"] == "completed", job["error"]
    before = upscale.probe_video(source)
    after = upscale.probe_video(Path(job["output_path"]))
    for key in ("width", "height", "fps", "frames", "has_audio"):
        assert after[key] == before[key]
    reopened = upscale.VideoUpscaleManager(tmp_path, database)
    assert reopened.get(job["id"])["status"] == "completed"
    assert reopened.list()[0]["id"] == job["id"]


def test_local_folder_batch_outputs_each_source_spec_and_skips_finished(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name, width, height, fps in (("clip.mp4", 120, 200, 12), ("clip.mov", 160, 90, 10)):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=blue:s={width}x{height}:r={fps}:d={3/fps}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:duration={3/fps}",
                        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        str(source / name)], check=True)
    monkeypatch.setattr(upscale, "model_status", lambda _directory, _model="x2plus": {"available": True})
    monkeypatch.setattr(upscale, "_load_model", lambda _directory, _model="x2plus": (None, "input", "output"))
    monkeypatch.setattr(upscale, "_upscale_frame", lambda frame, *_args: frame.tobytes())
    manager = upscale.VideoUpscaleManager(tmp_path, SQLiteStore(tmp_path / "batch.db"))
    preview = manager.preview_directory(str(source), model_name="x2plus")
    assert preview["pending_count"] == 2
    assert len({item["output_path"] for item in preview["items"]}) == 2
    assert preview["output_directory"] == str(source / "已处理")
    assert not (source / "已处理").exists()
    job = manager.create_batch(str(source), model_name="x2plus")
    assert job["kind"] == "batch" and job["total_files"] == 2
    deadline = time.monotonic() + 15
    while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.05)
        job = manager.get(job["id"])
    assert job["status"] == "completed", job
    assert job["completed_files"] == 2
    for item in job["items"]:
        before = upscale.probe_video(Path(item["source"]))
        after = upscale.probe_video(Path(item["output_path"]))
        for key in ("width", "height", "fps", "frames", "has_audio"):
            assert after[key] == before[key]
    assert manager.preview_directory(str(source), model_name="x2plus")["skipped_count"] == 2
