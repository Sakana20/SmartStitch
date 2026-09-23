from __future__ import annotations

import subprocess
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

import smartstitch.cluster as cluster
from smartstitch.api import create_app
from smartstitch.models import JobCreateRequest


def test_cluster_paths_follow_each_macs_nas_mount_alias():
    canonical = Path("/Volumes/home/Smartstitch")
    first = Path("/Volumes/homes/alice/Smartstitch")
    second = Path("/Volumes/homes/bob/Smartstitch")
    payload = {"source_root": str(first), "asset": {"path": str(first / "素材" / "1.mp4")}}
    transferred = cluster._canonicalize(payload, first, canonical)
    assert transferred["asset"]["path"] == str(canonical / "素材" / "1.mp4")
    localized = cluster._localize(transferred, canonical, second)
    assert localized == {"source_root": str(second), "asset": {"path": str(second / "素材" / "1.mp4")}}


def _clip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=120x200:r=20:d=0.2",
        "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=44100:duration=0.2",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    ], check=True)


def test_cluster_renders_one_item_using_worker_local_database(tmp_path, monkeypatch):
    shared = tmp_path / "nas" / "Smartstitch"
    shared.mkdir(parents=True)
    _clip(shared / "source" / "pool_1" / "one.mp4")
    config = {
        "schema_version": 3, "workflow_type": "generic", "id": "cluster-test", "name": "集群测试",
        "source_root": str(shared / "source"), "timeline": ["pool_1"],
        "sources": {"pool_1": {"label": "素材", "mode": "required", "directory": "pool_1"}},
        "benefit_overlays": {"mode": "disabled", "file": ""},
        "output": {"directory": str(shared / "output"), "width": 120, "height": 200,
                   "fps": 20, "video_codec": "libx264", "video_preset": "ultrafast", "crf": 30},
        "batch": {"concurrency": 1, "retry_count": 0, "minimum_free_space_gb": 0},
    }
    (shared / "cluster-test.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    master_app = create_app(base_directory=tmp_path, config_directory=shared, data_directory=tmp_path / "master-data")
    worker_app = create_app(base_directory=tmp_path, config_directory=shared, data_directory=tmp_path / "worker-data")
    worker_app.state.user_profiles.update("张三")
    worker = worker_app.state.cluster_worker
    peer_client = TestClient(worker._app())
    assert peer_client.get("/hello").status_code == 200
    worker.set_token_required(True)
    assert peer_client.get("/hello").status_code == 401
    assert peer_client.get("/hello", headers={"Authorization": f"Bearer {worker.settings['token']}"}).status_code == 200
    worker.set_token_required(False)
    submitted = []

    def local_request(url, token, *, method="GET", payload=None):
        path = url.removeprefix("http://127.0.0.1:9876")
        if path == "/attempts" and payload is not None:
            submitted.append(payload)
        headers = {"Authorization": f"Bearer {token}"}
        response = peer_client.request(method, path, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(cluster, "_request", local_request)
    try:
        master = master_app.state.cluster_master
        node = master.add_node("http://127.0.0.1:9876", "")
        assert node["node_id"] == worker.settings["node_id"]
        assert master.nodes[0]["token"] == ""
        assert node["display_name"] == "张三"
        assert master.node_statuses()[0]["display_name"] == "张三"
        worker_app.state.user_profiles.update("李四")
        assert master.node_statuses()[0]["display_name"] == "李四"
        assert worker.get("bad") is None
        job = master.create(JobCreateRequest(config_id="cluster-test", count=1, seed=7, auto_start=True))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = master_app.state.job_manager.get_job(job["id"])
            if job["status"] in {"completed", "failed", "partial_failed"}:
                break
            time.sleep(0.1)
        assert job["status"] == "completed", job["items"]
        assert job["items"][0]["worker_id"] == worker.settings["node_id"]
        assert Path(job["items"][0]["output_path"]).is_file()
        attempt = worker.get(job["items"][0]["attempt_id"])
        assert attempt["status"] == "succeeded"
        assert worker_app.state.job_manager.list_jobs() == []
        worker_app.state.accounts.bootstrap("admin", "管理员", "ClusterTest123@")
        worker_client = TestClient(worker_app)
        assert worker_client.post("/api/v1/auth/login", json={
            "username": "admin", "password": "ClusterTest123@",
        }).status_code == 200
        worker_records = worker_client.get("/api/v1/tools/cluster-worker/attempts").json()
        assert worker_records[0]["attempt_id"] == attempt["attempt_id"]
        assert worker_records[0]["config_name"] == "集群测试"
        assert worker_records[0]["output_name"] == job["items"][0]["output_name"]
        assert worker_records[0]["created_at"]
        duplicate = peer_client.post("/attempts", json=submitted[0], headers={"Authorization": f"Bearer {worker.settings['token']}"})
        assert duplicate.json() == {"attempt_id": attempt["attempt_id"], "status": "succeeded"}
        assert (Path(job["output_directory"]) / "manifest.csv").is_file()
        master_client = TestClient(master_app)
        assert master_client.post("/api/v1/auth/login", json={
            "username": "admin", "password": "ClusterTest123@",
        }).status_code == 200
        assert master_client.post(f"/api/v1/jobs/{job['id']}/cancel").status_code == 409
        assert master_client.delete(f"/api/v1/jobs/{job['id']}").status_code == 200
    finally:
        master_app.state.cluster_master.shutdown()
        worker_app.state.cluster_master.shutdown()
        master_app.state.instance_lock.release()
        worker_app.state.instance_lock.release()
