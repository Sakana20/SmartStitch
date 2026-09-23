from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def authenticated_client(app):
    """Create a browser client with a real administrator login for API tests."""
    if not app.state.accounts.initialized():
        app.state.accounts.bootstrap("testadmin", "测试管理员", "test-password-strong-123")
    client = TestClient(app)
    response = client.post("/api/v1/auth/login", json={
        "username": "testadmin", "password": "test-password-strong-123",
    })
    assert response.status_code == 200, response.text
    return client


@pytest.fixture(autouse=True)
def isolate_runtime_environment(tmp_path, monkeypatch):
    test_home = tmp_path / "home"
    test_home.mkdir()
    monkeypatch.setenv("HOME", str(test_home))
    monkeypatch.delenv("SMARTSTITCH_CONFIG_DIRECTORY", raising=False)
    monkeypatch.delenv("SMARTSTITCH_DATA_DIRECTORY", raising=False)
