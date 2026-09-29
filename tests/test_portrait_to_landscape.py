from pathlib import Path

import pytest

from conftest import authenticated_client
from smartstitch.api import create_app
from smartstitch.database import SQLiteStore
from smartstitch.landscape_to_portrait import inspect
from smartstitch.portrait_to_landscape import PortraitToLandscapeManager
from test_landscape_to_portrait import clip, wait, pytestmark


@pytest.mark.parametrize("resolution,size", [("720p", (1280, 720)), ("1080p", (1920, 1080))])
def test_batch_api_geometry_audio_names_and_history(tmp_path, resolution, size):
    (tmp_path / "config").mkdir()
    source = tmp_path / "含 空格素材"
    source.mkdir()
    clip(source / "竖屏.mp4", size="90x160", audio=True)
    clip(source / "竖屏.mov", size="90x160")
    clip(source / "横屏.mp4")
    clip(source / "方形.mp4", size="90x90")
    (source / "损坏.mp4").write_text("broken")
    (source / "旧_竖改横_2.mp4").write_text("generated")
    (source / "._伴生.mp4").write_text("hidden")
    (source / "sub").mkdir()
    clip(source / "sub" / "nested.mp4", size="90x160")
    output = source / "竖改横"
    output.mkdir()
    existing = output / "竖屏_竖改横.mp4"
    existing.write_bytes(b"keep existing")
    app = create_app(tmp_path)
    client = authenticated_client(app)
    manager = app.state.landscape_manager
    base = "/api/v1/tools/portrait-to-landscape"
    try:
        response = client.post(base + "/preview", json={"source_directory": str(source), "resolution": resolution})
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["pending_count"] == 5
        assert (preview["output_width"], preview["output_height"]) == size
        assert all("width" not in item for item in preview["items"])
        # Preflight IDs cannot be submitted to the other direction's manager.
        assert client.post("/api/v1/tools/landscape-to-portrait", json={"preview_id": preview["id"]}).status_code == 422
        created = client.post(base, json={"preview_id": preview["id"]})
        assert created.status_code == 200, created.text
        assert client.post(base, json={"preview_id": preview["id"]}).status_code == 422
        job = wait(manager, created.json()["id"])
        assert (job["succeeded"], job["skipped_count"], job["invalid_count"], job["completed"]) == (2, 2, 1, 5), job
        results = [item for item in job["items"] if item["status"] == "completed"]
        assert {Path(item["output_path"]).name for item in results} == {"竖屏_竖改横_2.mp4", "竖屏_竖改横_3.mp4"}
        for item in results:
            info = inspect(Path(item["output_path"]))
            assert (info["width"], info["height"]) == size
            assert info["has_audio"] == item["name"].endswith(".mp4")
            assert info["fps"] == "25"
        assert existing.read_bytes() == b"keep existing"
        assert client.get(base + "/jobs").json()[0]["id"] == job["id"]
        assert client.get("/api/v1/tools/landscape-to-portrait/jobs").json() == []
        assert client.get(base + "/" + job["id"]).json()["resolution"] == resolution
        assert PortraitToLandscapeManager(app.state.database_store).get(job["id"])["status"] == "completed"
        assert client.post(base + "/" + job["id"] + "/cancel").status_code == 200
        assert client.delete(base + "/" + job["id"]).status_code == 200
        assert all(Path(item["output_path"]).exists() for item in results)
        assert client.get(base + "/" + job["id"]).status_code == 404
        assert not list(output.glob(".*.tmp.mp4"))
    finally:
        manager.shutdown()
        app.state.portrait_manager.shutdown()
        app.state.instance_lock.release()


def test_clean_center_and_blurred_sides(tmp_path):
    import numpy as np
    import subprocess

    source = tmp_path / "clip.mp4"
    clip(source, size="90x160")
    manager = PortraitToLandscapeManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        job = wait(manager, manager.create(manager.preview(str(tmp_path))["id"])["id"])
        assert job["succeeded"] == 1, job
        result = Path(job["items"][0]["output_path"])
        def pixels(path, graph):
            return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", graph,
                                   "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                  capture_output=True, check=True).stdout
        expected = np.frombuffer(pixels(source, "scale=404:720"), dtype=np.uint8).astype(float)
        center = np.frombuffer(pixels(result, "crop=404:720:438:0"), dtype=np.uint8).astype(float)
        assert np.abs(expected - center).mean() < 12
        full = np.frombuffer(pixels(result, "null"), dtype=np.uint8).reshape(720, 1280, 3).astype(float)
        assert np.abs(np.diff(full[:, 30:330], axis=0)).mean() < np.abs(np.diff(full[:, 450:800], axis=0)).mean()
    finally:
        manager.shutdown()


def test_preflight_only_lists_and_rejects_changed_names(tmp_path, monkeypatch):
    import smartstitch.landscape_to_portrait as shared
    (tmp_path / "video.mp4").write_text("not probed yet")
    manager = PortraitToLandscapeManager(SQLiteStore(tmp_path / "jobs.db"))
    def no_probe(*args):
        pytest.fail("preflight must not probe")
    monkeypatch.setattr(shared, "probe", no_probe)
    preview = manager.preview(str(tmp_path))
    assert preview["output_directory"] == str(tmp_path / "竖改横")
    assert not (tmp_path / "竖改横").exists()
    (tmp_path / "added.mov").write_text("added")
    with pytest.raises(ValueError, match="已变化"):
        manager.create(preview["id"])
    with pytest.raises(ValueError, match="分辨率"):
        manager.preview(str(tmp_path), resolution="4k")


def test_cancel_during_inspection_and_restart_interruption(tmp_path, monkeypatch):
    import threading
    import smartstitch.landscape_to_portrait as shared

    clip(tmp_path / "clip.mp4", size="90x160")
    store = SQLiteStore(tmp_path / "jobs.db")
    manager = PortraitToLandscapeManager(store)
    entered = threading.Event()
    release = threading.Event()
    original = shared.inspect
    def blocked(path):
        entered.set()
        assert release.wait(5)
        return original(path)
    monkeypatch.setattr(shared, "inspect", blocked)
    try:
        job_id = manager.create(manager.preview(str(tmp_path))["id"])["id"]
        assert entered.wait(5)
        assert manager.active_count() == 1
        with pytest.raises(ValueError, match="运行中的任务"):
            manager.delete(job_id)
        assert manager.cancel(job_id)["status"] == "cancelling"
        release.set()
        job = wait(manager, job_id)
        assert job["status"] == "cancelled"
        assert job["items"][0]["status"] == "cancelled"
        assert not list((tmp_path / "竖改横").iterdir())
        manager.worker.join(5)
        assert manager.active_count() == 0
        job.update(id="unfinished", status="running")
        job["items"][0]["status"] = "running"
        store.save("landscape_jobs", job)
        restarted = PortraitToLandscapeManager(store)
        restored = restarted.get("unfinished")
        assert restored["status"] == "interrupted"
        assert restored["items"][0]["status"] == "interrupted"
    finally:
        release.set()
        manager.shutdown()
