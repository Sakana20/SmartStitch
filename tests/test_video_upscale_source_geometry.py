from __future__ import annotations

from conftest import authenticated_client

import subprocess
import time
from pathlib import Path

import pytest

import smartstitch.video_upscale as upscale
from smartstitch.api import create_app


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

    manager = upscale.VideoUpscaleManager(tmp_path)
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
