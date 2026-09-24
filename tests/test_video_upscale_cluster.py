from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from conftest import authenticated_client

import smartstitch.cluster as cluster
import smartstitch.cluster_upscale as cluster_upscale
from smartstitch.api import create_app
from smartstitch.video_upscale import MODEL_CONFIGS, probe_video


@pytest.mark.parametrize("model_name", ["x2plus", "animevideo"])
@pytest.mark.parametrize("shared_input,use_batch", [(False, False), (True, False), (True, True)])
def test_upscale_cluster_stages_and_publishes_exact_frames(tmp_path, monkeypatch, model_name, shared_input, use_batch):
    shared = tmp_path / "nas" / "Smartstitch"
    shared.mkdir(parents=True)
    source = shared / "超分" / "原素材" / "shared.mp4" if shared_input else tmp_path / "outside-nas.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    if shared_input:
        (shared / "超分" / "已处理").mkdir(parents=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=120x200:r=20:d=0.3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(source)], check=True)
    master_app = create_app(base_directory=tmp_path, config_directory=shared, data_directory=tmp_path / "master")
    worker_app = create_app(base_directory=tmp_path, config_directory=shared, data_directory=tmp_path / "worker")
    worker = worker_app.state.cluster_worker
    client = TestClient(worker._app())
    assignments = []

    def ready(_directory, model_name="x2plus"):
        return {"available": True, "model": model_name, "model_sha256": MODEL_CONFIGS[model_name]["sha256"],
                "platform_supported": True}

    def fake_segment(source, output, info, start, end, data_directory, cancelled, progress=None, process_callback=None, model_name="x2plus"):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                        "-vf", f"select='gte(n,{start})*lt(n,{end})'", "-vsync", "0",
                        "-frames:v", str(end-start), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-bf", "0", str(output)], check=True)
        if progress:
            progress(end-start)
        return {"frames": end-start, "output_path": str(output), "size_bytes": output.stat().st_size}

    def local_request(url, token, *, method="GET", payload=None):
        path = url.removeprefix("http://127.0.0.1:9876")
        if path == "/upscale-attempts" and payload is not None:
            assignments.append(payload)
        response = client.request(method, path, headers={"Authorization": f"Bearer {token}"}, json=payload)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(cluster, "model_status", ready)
    monkeypatch.setattr(cluster, "process_segment", fake_segment)
    monkeypatch.setattr(cluster, "_request", local_request)
    monkeypatch.setattr(cluster_upscale, "_request", local_request)
    try:
        master = master_app.state.cluster_master
        master.add_node("http://127.0.0.1:9876", worker.settings["token"])
        manager = master_app.state.cluster_upscale_batch_manager if use_batch else master_app.state.cluster_upscale_manager
        if use_batch:
            preview = manager.preview(model_name)
            assert preview["pending_count"] == 1
            assert preview["output_directory"] == str(shared / "超分" / "已处理")
            job = manager.create(model_name)
        else:
            job = manager.create(str(source), str(tmp_path), model_name)
        assert job["model"] == model_name
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = manager.get(job["id"])
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert job["status"] == "completed", job.get("error")
        output = Path(job["items"][0]["output_path"] if use_batch else job["output_path"])
        assert output.is_file()
        original, actual = probe_video(source), probe_video(output)
        assert (actual["width"], actual["height"], actual["frames"], actual["has_audio"]) == (
            original["width"], original["height"], original["frames"], original["has_audio"])
        assert not (shared / ".video-upscale" / assignments[0]["job_id"]).exists()
        assert assignments[0]["model"] == model_name
        assert assignments[0]["source_relative"] == (
            "超分/原素材/shared.mp4" if shared_input else f".video-upscale/{job['id']}/source.mp4")
        assert assignments[0]["model_sha256"] == MODEL_CONFIGS[model_name]["sha256"]
        if use_batch:
            assert manager.preview(model_name)["skipped_count"] == 1
            while (manager.active_count() or master_app.state.cluster_upscale_manager.active_count()) and time.monotonic() < deadline:
                time.sleep(0.02)
            browser = authenticated_client(master_app)
            queued = browser.get("/api/v1/tools/video-upscale/jobs").json()
            assert [entry["id"] for entry in queued] == [job["id"]]
            assert browser.delete(f"/api/v1/tools/video-upscale/{job['id']}").status_code == 200
            assert browser.get("/api/v1/tools/video-upscale/jobs").json() == []
        changed = {**assignments[0], "start": assignments[0]["start"] + 1}
        response = client.post("/upscale-attempts", json=changed,
                               headers={"Authorization": f"Bearer {worker.settings['token']}"})
        assert response.status_code == 422
    finally:
        master_app.state.cluster_upscale_batch_manager.shutdown()
        worker_app.state.cluster_upscale_batch_manager.shutdown()
        master_app.state.cluster_upscale_manager.shutdown()
        worker_app.state.cluster_upscale_manager.shutdown()
        master_app.state.cluster_master.shutdown()
        worker_app.state.cluster_master.shutdown()
        master_app.state.instance_lock.release()
        worker_app.state.instance_lock.release()
