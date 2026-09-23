from __future__ import annotations

from fastapi.testclient import TestClient

from smartstitch.api import create_app


def login(client: TestClient, username: str, password: str) -> None:
    result = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert result.status_code == 200, result.text


def test_login_roles_and_cross_machine_suspension(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    app_a = create_app(root_a, config_directory=shared)
    app_b = create_app(root_b, config_directory=shared)
    admin_a, user_b = TestClient(app_a), TestClient(app_b)
    assert admin_a.get("/api/v1/configs").status_code == 401
    assert admin_a.get("/api/v1/admin/users").status_code == 401
    app_a.state.accounts.bootstrap("admin", "管理员", "password-strong-123")
    login(admin_a, "admin", "password-strong-123")
    assert app_a.state.cluster_worker.status()["display_name"] == "管理员"
    created = admin_a.post("/api/v1/admin/users", json={
        "username": "editor", "display_name": "剪辑员", "password": "password-strong-456",
    })
    assert created.status_code == 200, created.text
    account_id = created.json()["account_id"]
    listed = admin_a.get("/api/v1/admin/users").json()
    assert all("password_hash" not in item and "auth_version" not in item for item in listed)
    login(user_b, "editor", "password-strong-456")
    assert app_b.state.cluster_worker.status()["display_name"] == "剪辑员"
    assert user_b.get("/api/v1/admin/users").status_code == 403
    assert user_b.get("/api/v1/tools/cluster-control/status").status_code == 403
    assert user_b.get("/api/v1/configs").status_code == 403  # Initial password must change.
    changed = user_b.post("/api/v1/auth/password", json={
        "old_password": "password-strong-456", "new_password": "password-strong-789",
    })
    assert changed.status_code == 200
    login(user_b, "editor", "password-strong-789")
    assert user_b.get("/api/v1/configs").status_code == 200
    assert user_b.get("/api/v1/tools/cluster-worker/attempts").status_code == 200
    suspended = admin_a.post(f"/api/v1/admin/users/{account_id}/actions", json={"action": "suspend"})
    assert suspended.status_code == 200
    assert user_b.get("/api/v1/configs").status_code == 401
    assert user_b.post("/api/v1/auth/login", json={"username": "editor", "password": "password-strong-789"}).status_code == 401


def test_last_admin_and_bootstrap_are_protected(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    client = TestClient(app)
    admin = app.state.accounts.bootstrap("admin", "管理员", "password-strong-123")
    login(client, "admin", "password-strong-123")
    for action in ("suspend", "demote", "delete"):
        result = client.post(f"/api/v1/admin/users/{admin['account_id']}/actions", json={"action": action})
        assert result.status_code == 409
    assert client.post("/api/v1/tools/cluster-control/unlock", json={"password": "SmartStitch123@"}).status_code == 404
    try:
        app.state.accounts.bootstrap("other", "其他", "password-strong-456")
    except Exception as exc:
        assert getattr(exc, "status", None) == 409
    else:
        raise AssertionError("bootstrap must be one-time")


def test_explicit_short_bootstrap_does_not_relax_new_user_passwords(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    app.state.accounts.bootstrap("admin", "管理员", "short", allow_short_password=True)
    client = TestClient(app)
    login(client, "admin", "short")
    response = client.post("/api/v1/admin/users", json={
        "username": "editor", "display_name": "剪辑员", "password": "short",
    })
    assert response.status_code == 400
    accepted = client.post("/api/v1/admin/users", json={
        "username": "editor", "display_name": "剪辑员", "password": "sixsix",
    })
    assert accepted.status_code == 200
    account_id = accepted.json()["account_id"]
    reset = client.post(f"/api/v1/admin/users/{account_id}/actions", json={
        "action": "reset_password", "value": "new678",
    })
    assert reset.status_code == 200
    editor = TestClient(app)
    login(editor, "editor", "new678")
    changed = editor.post("/api/v1/auth/password", json={
        "old_password": "new678", "new_password": "abc123",
    })
    assert changed.status_code == 200
    login(editor, "editor", "abc123")
