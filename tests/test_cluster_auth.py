from __future__ import annotations

from fastapi.testclient import TestClient

from smartstitch.api import create_app


def authenticated_client(tmp_path):
    app = create_app(tmp_path)
    if not app.state.accounts.initialized():
        app.state.accounts.bootstrap("admin", "管理员", "ClusterTest123@")
    client = TestClient(app)
    assert client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "ClusterTest123@",
    }).status_code == 200
    return client


def test_cluster_control_requires_admin_login(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    client = TestClient(app)
    status_url = "/api/v1/tools/cluster-control/status"

    assert client.get(status_url).status_code == 401
    assert client.get("/api/v1/tools/cluster-control/discover").status_code == 401
    assert client.post("/api/v1/tools/cluster-control/worker/start").status_code == 401
    assert client.post("/api/v1/tools/cluster-control/worker/token-protection", json={"enabled": True}).status_code == 401
    assert client.post("/api/v1/tools/cluster-control/jobs", json={"config_id": "x", "count": 1}).status_code == 401
    app.state.accounts.bootstrap("admin", "管理员", "ClusterTest123@")
    assert client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "ClusterTest123@",
    }).status_code == 200
    status = client.get(status_url).json()
    assert status["available"] is True
    assert status["worker"]["enabled"] is False
    assert status["worker"]["token_required"] is False
    assert status["nodes"] == []
    assert client.post("/api/v1/auth/logout").json() == {"ok": True}
    assert client.get(status_url).status_code == 401


def test_short_worker_token_explains_which_token_to_use(tmp_path):
    (tmp_path / "config").mkdir()
    client = authenticated_client(tmp_path)
    response = client.post(
        "/api/v1/tools/cluster-control/nodes",
        json={"url": "http://127.0.0.1:8767", "token": "SmartStitch123@"},
    )
    assert response.status_code == 422
    assert "工作机启用后显示的随机令牌" in response.json()["detail"][0]["msg"]


def test_worker_starts_on_each_app_launch_and_token_protection_is_optional(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    starts = []

    def record_start(worker):
        starts.append(worker.settings["enabled"])
        return {}

    monkeypatch.setattr("smartstitch.cluster.ClusterWorker.start", record_start)
    for launch in range(2):
        app = create_app(tmp_path)
        if not app.state.accounts.initialized():
            app.state.accounts.bootstrap("admin", "管理员", "ClusterTest123@")
        with TestClient(app) as client:
            assert client.post("/api/v1/auth/login", json={
                "username": "admin", "password": "ClusterTest123@",
            }).status_code == 200
            status = client.get("/api/v1/tools/cluster-control/status").json()["worker"]
            assert len(starts) == launch + 1
            assert status["token_required"] is (launch == 1)
            if launch == 0:
                changed = client.post(
                    "/api/v1/tools/cluster-control/worker/token-protection",
                    json={"enabled": True},
                )
                assert changed.json() == {"token_required": True}
                assert client.post("/api/v1/tools/cluster-control/worker/stop").json() == {"enabled": False}
    assert starts == [True, False]
