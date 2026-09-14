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
