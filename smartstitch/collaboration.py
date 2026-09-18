from __future__ import annotations

import json
import re
import shutil
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import ConfigStore


CONFIG_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULT_LEASE_SECONDS = 120
COMMIT_LEASE_SECONDS = 60


class CollaborationError(RuntimeError):
    pass


class UserProfileRequiredError(CollaborationError):
    pass


class ConfigLockHeldError(CollaborationError):
    def __init__(self, status: dict[str, Any]):
        self.status = status
        owner = status.get("owner") or {}
        name = owner.get("display_name") or "其他同事"
        device = owner.get("device_name")
        location = f"（{device}）" if device else ""
        super().__init__(f"{name}{location}已打开此配置，请先联系同事保存关闭")


class ConfigLeaseError(CollaborationError):
    pass


class ConfigVersionConflictError(CollaborationError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class UserProfileStore:
    def __init__(self, data_directory: Path):
        self.data_directory = data_directory
        self.profile_path = data_directory / "user-profile.json"
        self.instance_path = data_directory / "service-instance.json"
        self.lock = threading.RLock()
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self._ensure_instance()

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollaborationError(f"无法读取本机用户资料: {exc}") from exc
        if not isinstance(value, dict):
            raise CollaborationError(f"本机资料格式错误: {path.name}")
        return value

    def _ensure_instance(self) -> dict[str, Any]:
        with self.lock:
            existing = self._read_json(self.instance_path)
            if existing is not None:
                return existing
            instance = {
                "schema_version": 1,
                "service_instance_id": uuid.uuid4().hex,
                "device_name": socket.gethostname() or "SmartStitch 电脑",
            }
            _write_json_atomic(self.instance_path, instance)
            return instance

    def get(self) -> dict[str, Any]:
        with self.lock:
            profile = self._read_json(self.profile_path)
            instance = self._ensure_instance()
            return {
                "configured": profile is not None,
                "user": profile,
                "device": instance,
            }

    def require(self) -> tuple[dict[str, Any], dict[str, Any]]:
        current = self.get()
        profile = current["user"]
        if not isinstance(profile, dict):
            raise UserProfileRequiredError("请先输入用户名")
        return profile, current["device"]

    def update(self, display_name: str, *, switch_user: bool = False) -> dict[str, Any]:
        name = display_name.strip()
        if not name:
            raise CollaborationError("用户名不能为空")
        if len(name) > 50:
            raise CollaborationError("用户名不能超过 50 个字符")
        with self.lock:
            existing = self._read_json(self.profile_path)
            now = _utc_now()
            profile = {
                "schema_version": 1,
                "user_id": (
                    str(existing.get("user_id"))
                    if existing is not None and not switch_user
                    else uuid.uuid4().hex
                ),
                "display_name": name,
                "created_at": (
                    str(existing.get("created_at"))
                    if existing is not None and not switch_user
                    else now
                ),
                "updated_at": now,
            }
            _write_json_atomic(self.profile_path, profile)
            return self.get()


class ConfigLeaseManager:
    def __init__(
        self,
        config_store: ConfigStore,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ):
        self.config_store = config_store
        self.root = config_store.directory / ".smartstitch-locks"
        self.lease_seconds = lease_seconds

    def _validate_config_id(self, config_id: str) -> None:
        if not CONFIG_ID_PATTERN.fullmatch(config_id):
            raise CollaborationError("配置 ID 格式不正确")

    def _edit_path(self, config_id: str) -> Path:
        self._validate_config_id(config_id)
        return self.root / f"{config_id}.edit.lock"

    def _commit_path(self, config_id: str) -> Path:
        self._validate_config_id(config_id)
        return self.root / f"{config_id}.commit.lock"

    @staticmethod
    def _read_owner(directory: Path) -> dict[str, Any] | None:
        try:
            value = json.loads((directory / "owner.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return value if isinstance(value, dict) else None

    def _is_stale(self, directory: Path, owner: dict[str, Any] | None) -> bool:
        now = time.time()
        if owner is not None:
            heartbeat = owner.get("last_heartbeat_epoch")
            seconds = owner.get("lease_seconds", self.lease_seconds)
            if isinstance(heartbeat, (int, float)) and isinstance(seconds, (int, float)):
                if heartbeat > now + seconds:
                    return False
                return now - heartbeat > seconds
        try:
            return now - directory.stat().st_mtime > self.lease_seconds
        except FileNotFoundError:
            return True

    @staticmethod
    def _public_owner(owner: dict[str, Any] | None) -> dict[str, Any] | None:
        if owner is None:
            return None
        return {
            key: value
            for key, value in owner.items()
            if key not in {"lease_token", "last_heartbeat_epoch"}
        }

    def status(self, config_id: str) -> dict[str, Any]:
        directory = self._edit_path(config_id)
        if not directory.is_dir():
            return {"locked": False, "stale": False, "owner": None}
        owner = self._read_owner(directory)
        return {
            "locked": True,
            "stale": self._is_stale(directory, owner),
            "owner": self._public_owner(owner),
        }

    @contextmanager
    def commit_guard(self, config_id: str) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self._commit_path(config_id)
        token = uuid.uuid4().hex
        deadline = time.monotonic() + 2
        while True:
            try:
                directory.mkdir()
                break
            except FileExistsError:
                owner = self._read_owner(directory)
                if self._is_stale(directory, owner):
                    stale = directory.with_name(
                        f"{directory.name}.stale-{uuid.uuid4().hex}"
                    )
                    try:
                        directory.replace(stale)
                    except FileNotFoundError:
                        continue
                    continue
                if time.monotonic() >= deadline:
                    raise ConfigLeaseError("配置正在提交，请稍后重试")
                time.sleep(0.05)
        owner = {
            "schema_version": 1,
            "lease_token": token,
            "acquired_at": _utc_now(),
            "last_heartbeat_at": _utc_now(),
            "last_heartbeat_epoch": time.time(),
            "lease_seconds": COMMIT_LEASE_SECONDS,
        }
        try:
            _write_json_atomic(directory / "owner.json", owner)
            yield
        finally:
            current = self._read_owner(directory)
            if current is not None and current.get("lease_token") == token:
                released = directory.with_name(
                    f"{directory.name}.released-{uuid.uuid4().hex}"
                )
                try:
                    directory.replace(released)
                except FileNotFoundError:
                    pass
                else:
                    shutil.rmtree(released, ignore_errors=True)
            elif current is None:
                try:
                    directory.rmdir()
                except OSError:
                    pass

    def _new_owner(
        self,
        config_id: str,
        profile: dict[str, Any],
        device: dict[str, Any],
        browser_session_id: str,
    ) -> dict[str, Any]:
        now_iso = _utc_now()
        return {
            "schema_version": 1,
            "config_id": config_id,
            "display_name": profile["display_name"],
            "user_id": profile["user_id"],
            "device_name": device["device_name"],
            "service_instance_id": device["service_instance_id"],
            "browser_session_id": browser_session_id,
            "lease_token": uuid.uuid4().hex,
            "acquired_at": now_iso,
            "last_heartbeat_at": now_iso,
            "last_heartbeat_epoch": time.time(),
            "lease_seconds": self.lease_seconds,
            "base_config_hash": self.config_store.content_hash(config_id),
        }

    def _acquired_result(self, owner: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "locked": True,
            "stale": False,
            "lease_token": owner["lease_token"],
            "owner": self._public_owner(owner),
            "content_hash": owner["base_config_hash"],
        }

    def acquire(
        self,
        config_id: str,
        profile: dict[str, Any],
        device: dict[str, Any],
        browser_session_id: str,
        *,
        takeover: bool = False,
    ) -> dict[str, Any]:
        self.config_store.path_for(config_id).stat()
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self._edit_path(config_id)
        try:
            directory.mkdir()
        except FileExistsError:
            with self.commit_guard(config_id):
                existing = self._read_owner(directory)
                if existing is not None and all(
                    existing.get(key) == expected
                    for key, expected in {
                        "user_id": profile["user_id"],
                        "service_instance_id": device["service_instance_id"],
                        "browser_session_id": browser_session_id,
                    }.items()
                ):
                    existing["last_heartbeat_at"] = _utc_now()
                    existing["last_heartbeat_epoch"] = time.time()
                    _write_json_atomic(directory / "owner.json", existing)
                    return self._acquired_result(existing)
                status = self.status(config_id)
                if not status["stale"] or not takeover:
                    raise ConfigLockHeldError(status)
                stale = directory.with_name(
                    f"{directory.name}.stale-{uuid.uuid4().hex}"
                )
                directory.replace(stale)
                directory.mkdir()
                owner = self._new_owner(
                    config_id, profile, device, browser_session_id
                )
                _write_json_atomic(directory / "owner.json", owner)
                return self._acquired_result(owner)
        owner = self._new_owner(config_id, profile, device, browser_session_id)
        try:
            _write_json_atomic(directory / "owner.json", owner)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return self._acquired_result(owner)

    def _assert_owner(
        self, config_id: str, lease_token: str, browser_session_id: str | None = None
    ) -> dict[str, Any]:
        directory = self._edit_path(config_id)
        owner = self._read_owner(directory)
        if owner is None:
            raise ConfigLeaseError("配置编辑锁不存在或无法读取")
        if owner.get("lease_token") != lease_token:
            raise ConfigLeaseError("配置编辑权已属于其他会话")
        if (
            browser_session_id is not None
            and owner.get("browser_session_id") != browser_session_id
        ):
            raise ConfigLeaseError("当前标签页不是配置锁持有者")
        if self._is_stale(directory, owner):
            raise ConfigLeaseError("配置编辑锁已过期，请重新打开配置")
        return owner

    def renew(
        self, config_id: str, lease_token: str, browser_session_id: str
    ) -> dict[str, Any]:
        with self.commit_guard(config_id):
            owner = self._assert_owner(config_id, lease_token, browser_session_id)
            owner["last_heartbeat_at"] = _utc_now()
            owner["last_heartbeat_epoch"] = time.time()
            _write_json_atomic(self._edit_path(config_id) / "owner.json", owner)
            return self._acquired_result(owner)

    def release(
        self, config_id: str, lease_token: str, browser_session_id: str
    ) -> dict[str, Any]:
        with self.commit_guard(config_id):
            directory = self._edit_path(config_id)
            if not directory.exists():
                return {"ok": True, "released": False}
            self._assert_owner(config_id, lease_token, browser_session_id)
            released = directory.with_name(
                f"{directory.name}.released-{uuid.uuid4().hex}"
            )
            directory.replace(released)
            shutil.rmtree(released, ignore_errors=True)
            return {"ok": True, "released": True}

    @contextmanager
    def write_guard(
        self, config_id: str, lease_token: str, expected_hash: str
    ) -> Iterator[None]:
        if not lease_token:
            raise ConfigLeaseError("缺少配置编辑锁，请重新打开配置")
        if not expected_hash:
            raise ConfigVersionConflictError("缺少配置版本，请刷新后重试")
        with self.commit_guard(config_id):
            owner = self._assert_owner(config_id, lease_token)
            current_hash = self.config_store.content_hash(config_id)
            if current_hash != expected_hash:
                raise ConfigVersionConflictError("配置已被修改，请刷新后重试")
            yield
            try:
                owner["base_config_hash"] = self.config_store.content_hash(config_id)
            except FileNotFoundError:
                owner["base_config_hash"] = None
            owner["last_heartbeat_at"] = _utc_now()
            owner["last_heartbeat_epoch"] = time.time()
            edit_path = self._edit_path(config_id)
            current_owner = self._read_owner(edit_path)
            if (
                current_owner is not None
                and current_owner.get("lease_token") == lease_token
            ):
                _write_json_atomic(edit_path / "owner.json", owner)
