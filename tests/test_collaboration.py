from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from smartstitch.api import create_app
from smartstitch.collaboration import (
    ConfigLeaseError,
    ConfigLeaseManager,
    ConfigLockHeldError,
    ConfigVersionConflictError,
    UserProfileStore,
)
from smartstitch.config import ConfigStore


def write_config(directory: Path, config_id: str = "shared-config") -> None:
    payload = {
        "schema_version": 2,
        "id": config_id,
        "name": "共享配置",
        "source_root": str(directory),
        "timeline": ["hook", "benefit_1", "ending"],
        "sources": {
            category: {"mode": "required", "directory": category}
            for category in ["hook", "benefit_1", "ending"]
        },
        "benefit_overlays": {"mode": "disabled", "file": ""},
        "output": {"directory": str(directory / "output")},
    }
    (directory / f"{config_id}.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_user_profile_is_local_and_persistent(tmp_path):
    first = UserProfileStore(tmp_path / "data")
    created = first.update("张三")
    user_id = created["user"]["user_id"]
    instance_id = created["device"]["service_instance_id"]

    reopened = UserProfileStore(tmp_path / "data")
    assert reopened.get()["user"]["display_name"] == "张三"
    assert reopened.get()["user"]["user_id"] == user_id
    assert reopened.get()["device"]["service_instance_id"] == instance_id

    renamed = reopened.update("张三（白班）")
    assert renamed["user"]["user_id"] == user_id
    switched = reopened.update("李四", switch_user=True)
    assert switched["user"]["user_id"] != user_id


def test_two_service_instances_cannot_edit_same_config(tmp_path):
    config_directory = tmp_path / "nas"
    config_directory.mkdir()
    write_config(config_directory)
    store_a = ConfigStore(config_directory)
    store_b = ConfigStore(config_directory)
    manager_a = ConfigLeaseManager(store_a)
    manager_b = ConfigLeaseManager(store_b)
    profile_a = {"user_id": "user-a", "display_name": "张三"}
    profile_b = {"user_id": "user-b", "display_name": "李四"}
    device_a = {"service_instance_id": "machine-a", "device_name": "剪辑机 A"}
    device_b = {"service_instance_id": "machine-b", "device_name": "剪辑机 B"}

    acquired_a = manager_a.acquire(
        "shared-config", profile_a, device_a, "browser-session-a"
    )
    with pytest.raises(ConfigLockHeldError) as conflict:
        manager_b.acquire(
            "shared-config", profile_b, device_b, "browser-session-b"
        )
    assert conflict.value.status["owner"]["display_name"] == "张三"
    assert conflict.value.status["owner"]["device_name"] == "剪辑机 A"

    manager_a.renew(
        "shared-config", acquired_a["lease_token"], "browser-session-a"
    )
    manager_a.release(
        "shared-config", acquired_a["lease_token"], "browser-session-a"
    )
    acquired_b = manager_b.acquire(
        "shared-config", profile_b, device_b, "browser-session-b"
    )
    assert acquired_b["owner"]["display_name"] == "李四"


def test_write_guard_requires_owner_and_current_hash(tmp_path):
    config_directory = tmp_path / "nas"
    config_directory.mkdir()
    write_config(config_directory)
    store = ConfigStore(config_directory)
    manager = ConfigLeaseManager(store)
    acquired = manager.acquire(
        "shared-config",
        {"user_id": "user-a", "display_name": "张三"},
        {"service_instance_id": "machine-a", "device_name": "剪辑机 A"},
        "browser-session-a",
    )

    with pytest.raises(ConfigLeaseError):
        with manager.write_guard(
            "shared-config", "wrong-token", acquired["content_hash"]
        ):
            pass
    with pytest.raises(ConfigVersionConflictError):
        with manager.write_guard(
            "shared-config", acquired["lease_token"], "0" * 64
        ):
            pass

    config = store.load("shared-config")
    config.name = "安全保存"
    with manager.write_guard(
        "shared-config", acquired["lease_token"], acquired["content_hash"]
    ):
        store.save_config("shared-config", config)
    assert store.load("shared-config").name == "安全保存"


def test_api_reports_lock_owner_across_independent_services(tmp_path):
    shared = tmp_path / "nas"
    shared.mkdir()
    write_config(shared)
    root_a = tmp_path / "machine-a"
    root_b = tmp_path / "machine-b"
    client_a = TestClient(create_app(root_a, config_directory=shared))
    client_b = TestClient(create_app(root_b, config_directory=shared))
    client_a.put("/api/v1/users/me", json={"display_name": "张三"})
    client_b.put("/api/v1/users/me", json={"display_name": "李四"})

    acquired = client_a.post(
        "/api/v1/configs/shared-config/lock/acquire",
        json={"browser_session_id": "browser-session-a"},
    )
    assert acquired.status_code == 200
    blocked = client_b.post(
        "/api/v1/configs/shared-config/lock/acquire",
        json={"browser_session_id": "browser-session-b"},
    )
    assert blocked.status_code == 423
    assert blocked.json()["detail"]["owner"]["display_name"] == "张三"

    loaded = acquired.json()["config"]
    loaded["name"] = "张三保存"
    saved = client_a.put(
        "/api/v1/configs/shared-config/structured",
        json={"config": loaded},
        headers={
            "X-SmartStitch-Lease": acquired.json()["lease_token"],
            "X-SmartStitch-Config-Hash": acquired.json()["content_hash"],
        },
    )
    assert saved.status_code == 200
    assert yaml.safe_load((shared / "shared-config.yaml").read_text("utf-8"))["name"] == "张三保存"

    released = client_a.post(
        "/api/v1/configs/shared-config/lock/release",
        json={
            "browser_session_id": "browser-session-a",
            "lease_token": acquired.json()["lease_token"],
        },
    )
    assert released.status_code == 200
    assert client_b.post(
        "/api/v1/configs/shared-config/lock/acquire",
        json={"browser_session_id": "browser-session-b"},
    ).status_code == 200


def test_api_requires_user_and_lease_before_config_write(tmp_path):
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    write_config(config_directory)
    client = TestClient(create_app(tmp_path))

    missing_user = client.post(
        "/api/v1/configs/shared-config/lock/acquire",
        json={"browser_session_id": "browser-session-a"},
    )
    assert missing_user.status_code == 428
    assert missing_user.json()["detail"]["code"] == "user_required"

    loaded = client.get("/api/v1/configs/shared-config").json()
    loaded["config"]["name"] = "不应保存"
    missing_lease = client.put(
        "/api/v1/configs/shared-config/structured",
        json={"config": loaded["config"]},
        headers={"X-SmartStitch-Config-Hash": loaded["content_hash"]},
    )
    assert missing_lease.status_code == 423
    assert missing_lease.json()["detail"]["code"] == "lease_invalid"
    assert yaml.safe_load(
        (config_directory / "shared-config.yaml").read_text("utf-8")
    )["name"] == "共享配置"


def test_expired_lock_requires_explicit_takeover(tmp_path):
    config_directory = tmp_path / "nas"
    config_directory.mkdir()
    write_config(config_directory)
    store = ConfigStore(config_directory)
    manager_a = ConfigLeaseManager(store, lease_seconds=1)
    manager_b = ConfigLeaseManager(store, lease_seconds=1)
    manager_a.acquire(
        "shared-config",
        {"user_id": "user-a", "display_name": "张三"},
        {"service_instance_id": "machine-a", "device_name": "剪辑机 A"},
        "browser-session-a",
    )
    owner_path = (
        config_directory
        / ".smartstitch-locks"
        / "shared-config.edit.lock"
        / "owner.json"
    )
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["last_heartbeat_epoch"] = 0
    owner_path.write_text(json.dumps(owner), encoding="utf-8")

    with pytest.raises(ConfigLockHeldError) as blocked:
        manager_b.acquire(
            "shared-config",
            {"user_id": "user-b", "display_name": "李四"},
            {"service_instance_id": "machine-b", "device_name": "剪辑机 B"},
            "browser-session-b",
        )
    assert blocked.value.status["stale"] is True

    taken = manager_b.acquire(
        "shared-config",
        {"user_id": "user-b", "display_name": "李四"},
        {"service_instance_id": "machine-b", "device_name": "剪辑机 B"},
        "browser-session-b",
        takeover=True,
    )
    assert taken["owner"]["display_name"] == "李四"
