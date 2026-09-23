from __future__ import annotations

from fastapi.testclient import TestClient

from smartstitch.api import create_app
from smartstitch.cluster_auth import ClusterAccess, SESSION_SECONDS


def test_cluster_control_requires_password_and_revokes_session(tmp_path):
    (tmp_path / "config").mkdir()
    client = TestClient(create_app(tmp_path))
    status_url = "/api/v1/tools/cluster-control/status"
    unlock_url = "/api/v1/tools/cluster-control/unlock"
    lock_url = "/api/v1/tools/cluster-control/lock"

    assert client.get(status_url).status_code == 401
    assert client.post(unlock_url, json={"password": "wrong"}).status_code == 401

    unlocked = client.post(unlock_url, json={"password": "SmartStitch123@"})
    assert unlocked.status_code == 200
    token = unlocked.json()["access_token"]
    assert unlocked.json()["expires_in"] == SESSION_SECONDS
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(status_url, headers=headers).json()["available"] is False
    assert client.post(lock_url, headers=headers).json() == {"locked": True}
    assert client.get(status_url, headers=headers).status_code == 401


def test_cluster_password_can_be_overridden_and_session_expires(monkeypatch):
    monkeypatch.setenv("SMARTSTITCH_CLUSTER_PASSWORD", "changed-password")
    clock = {"now": 100.0}
    monkeypatch.setattr("smartstitch.cluster_auth.time.monotonic", lambda: clock["now"])
    access = ClusterAccess()

    assert access.unlock("SmartStitch123@") is None
    token = access.unlock("changed-password")
    assert token and access.authorized(token)
    clock["now"] += SESSION_SECONDS
    assert not access.authorized(token)
