from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from smartstitch.api import create_app


def test_health_and_config_listing(tmp_path):
    (tmp_path / "config").mkdir()
    client = TestClient(create_app(tmp_path))
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert client.get("/api/v1/configs").json() == []


def test_structured_config_update(tmp_path):
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    config = {
        "id": "visual-test",
        "name": "可视化测试",
        "source_root": str(tmp_path),
        "timeline": ["hook", "benefit_video", "ending"],
        "sources": {
            category: {"mode": "required", "directory": category}
            for category in ["hook", "benefit_video", "ending"]
        },
        "benefit_overlays": {"mode": "disabled", "directory": "overlay"},
        "output": {"directory": str(tmp_path / "output")},
    }
    (config_directory / "visual-test.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    client = TestClient(create_app(tmp_path))
    loaded = client.get("/api/v1/configs/visual-test").json()["config"]
    loaded["name"] = "网页修改后的配置"
    loaded["output"]["fps"] = 25
    response = client.put(
        "/api/v1/configs/visual-test/structured", json={"config": loaded}
    )
    assert response.status_code == 200
    saved = client.get("/api/v1/configs/visual-test").json()["config"]
    assert saved["name"] == "网页修改后的配置"
    assert saved["output"]["fps"] == 25
    assert list((config_directory / "backups").glob("*.yaml"))


def test_delete_job_record_keeps_active_jobs_protected(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    client = TestClient(app)
    manager = app.state.job_manager
    manager.database.save({"id": "finished-job", "status": "completed"})
    manager.database.save({"id": "running-job", "status": "running"})

    response = client.delete("/api/v1/jobs/finished-job")
    assert response.status_code == 200
    assert manager.database.get("finished-job") is None

    response = client.delete("/api/v1/jobs/running-job")
    assert response.status_code == 409
    assert manager.database.get("running-job") is not None

    response = client.delete("/api/v1/jobs")
    assert response.status_code == 409

    manager.database.save({"id": "running-job", "status": "completed"})
    manager.database.save({"id": "another-finished-job", "status": "failed"})
    response = client.delete("/api/v1/jobs")
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 2
    assert manager.database.list() == []
