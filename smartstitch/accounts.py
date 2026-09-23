"""Shared account registry and process-local browser sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class AccountError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash_password(password: str, *, allow_short: bool = False) -> str:
    if len(password) < 6 and not allow_short:
        raise AccountError("密码至少需要 6 个字符")
    if not password:
        raise AccountError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, digest = encoded.split("$")
        if algorithm != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(candidate, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def public_account(account: dict) -> dict:
    return {key: account.get(key) for key in (
        "account_id", "username", "display_name", "role", "status",
        "must_change_password", "created_at", "updated_at", "last_login_at",
    )}


class AccountStore:
    def __init__(self, config_directory: Path):
        self.directory = config_directory / ".smartstitch-accounts"
        self.path = self.directory / "accounts.json"
        self.lock_path = self.directory / "write.lock"
        self._local_lock = threading.RLock()
        self._sessions: dict[str, tuple[str, int, float, float]] = {}
        self._attempts: dict[str, tuple[int, float]] = {}

    def _read(self) -> dict:
        try:
            if not self.path.exists():
                return {"schema_version": 1, "accounts": [], "audit": []}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1 or not isinstance(data.get("accounts"), list):
                raise ValueError("账号注册表格式错误")
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise AccountError(f"无法读取账号注册表：{exc}", 503) from exc

    def initialized(self) -> bool:
        return bool(self._read()["accounts"])

    @contextmanager
    def _write_lock(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AccountError(f"无法访问账号目录：{exc}", 503) from exc
        with self._local_lock:
            deadline = time.monotonic() + 10
            while True:
                try:
                    self.lock_path.mkdir()
                    break
                except FileExistsError:
                    try:
                        if time.time() - self.lock_path.stat().st_mtime > 180:
                            stale = self.directory / f"write.lock.stale-{uuid.uuid4().hex}"
                            self.lock_path.replace(stale)
                            shutil.rmtree(stale, ignore_errors=True)
                            continue
                    except FileNotFoundError:
                        continue
                    if time.monotonic() >= deadline:
                        raise AccountError("账号注册表正被其他电脑修改，请稍后重试", 503)
                    time.sleep(0.05)
                except OSError as exc:
                    raise AccountError(f"无法锁定账号注册表：{exc}", 503) from exc
            try:
                yield
            finally:
                shutil.rmtree(self.lock_path, ignore_errors=True)

    def _write(self, data: dict) -> None:
        temporary = self.directory / f"accounts.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as file:
                os.chmod(temporary, 0o600)
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            if self.path.is_file():
                backup = self.directory / "accounts.json.bak"
                backup_temp = self.directory / f"accounts.{uuid.uuid4().hex}.bak.tmp"
                try:
                    shutil.copyfile(self.path, backup_temp)
                    os.chmod(backup_temp, 0o600)
                    backup_temp.replace(backup)
                finally:
                    backup_temp.unlink(missing_ok=True)
            temporary.replace(self.path)
        except OSError as exc:
            raise AccountError(f"无法写入账号注册表：{exc}", 503) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _username(value: str) -> str:
        name = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,31}", name):
            raise AccountError("用户名须为 3–32 位小写字母、数字、点、横线或下划线")
        return name

    @staticmethod
    def _account(data: dict, account_id: str) -> dict:
        account = next((item for item in data["accounts"] if item["account_id"] == account_id), None)
        if account is None or account["status"] == "deleted":
            raise AccountError("用户不存在", 404)
        return account

    @staticmethod
    def _require_admin(data: dict, account_id: str) -> None:
        actor = AccountStore._account(data, account_id)
        if actor["status"] != "active" or actor["role"] != "admin":
            raise AccountError("仅管理员可以操作", 403)

    def bootstrap(self, username: str, display_name: str, password: str, *, allow_short_password: bool = False) -> dict:
        with self._write_lock():
            data = self._read()
            if data["accounts"]:
                raise AccountError("管理员已经设置", 409)
            account = self._create(data, username, display_name, password, "admin", False, allow_short_password)
            self._write(data)
            return public_account(account)

    def _create(self, data: dict, username: str, display_name: str, password: str, role: str, temporary: bool, allow_short_password: bool = False) -> dict:
        name = self._username(username)
        if any(item["username"] == name for item in data["accounts"]):
            raise AccountError("用户名已存在或曾被使用", 409)
        if not display_name.strip() or len(display_name.strip()) > 50:
            raise AccountError("显示名称须为 1–50 个字符")
        if role not in ("admin", "user"):
            raise AccountError("角色无效")
        account = {"account_id": uuid.uuid4().hex, "username": name, "display_name": display_name.strip(),
                   "role": role, "status": "active", "password_hash": _hash_password(password, allow_short=allow_short_password),
                   "must_change_password": temporary, "auth_version": 1, "created_at": _now(),
                   "updated_at": _now(), "last_login_at": None}
        data["accounts"].append(account)
        return account

    def list_accounts(self) -> list[dict]:
        return [public_account(item) for item in self._read()["accounts"] if item["status"] != "deleted"]

    def create(self, actor_id: str, username: str, display_name: str, password: str, role: str) -> dict:
        with self._write_lock():
            data = self._read()
            self._require_admin(data, actor_id)
            account = self._create(data, username, display_name, password, role, True)
            self._audit(data, actor_id, account["account_id"], "create")
            self._write(data)
            return public_account(account)

    @staticmethod
    def _audit(data: dict, actor: str, target: str, action: str) -> None:
        data.setdefault("audit", []).append({"actor_id": actor, "target_id": target, "action": action, "at": _now()})

    def update(self, actor_id: str, account_id: str, *, action: str, value: str | None = None) -> dict:
        with self._write_lock():
            data = self._read()
            self._require_admin(data, actor_id)
            target = self._account(data, account_id)
            if action in ("suspend", "delete", "demote") and target["role"] == "admin" and target["status"] == "active":
                others = [a for a in data["accounts"] if a["account_id"] != account_id and a["role"] == "admin" and a["status"] == "active"]
                if not others:
                    raise AccountError("必须保留至少一名启用的管理员", 409)
            if action == "delete" and actor_id == account_id:
                raise AccountError("不能删除当前登录账号", 409)
            if action == "suspend": target["status"] = "suspended"
            elif action == "enable": target["status"] = "active"
            elif action == "delete": target["status"] = "deleted"
            elif action == "promote": target["role"] = "admin"
            elif action == "demote": target["role"] = "user"
            elif action == "reset_password":
                target["password_hash"] = _hash_password(value or "")
                target["must_change_password"] = True
            elif action == "rename":
                if not value or len(value.strip()) > 50: raise AccountError("显示名称须为 1–50 个字符")
                target["display_name"] = value.strip()
            else: raise AccountError("操作无效")
            target["auth_version"] += 1
            target["updated_at"] = _now()
            self._audit(data, actor_id, account_id, action)
            self._write(data)
            return public_account(target)

    def change_password(self, account_id: str, old_password: str, new_password: str) -> None:
        with self._write_lock():
            data = self._read()
            account = self._account(data, account_id)
            if not _verify_password(old_password, account["password_hash"]):
                raise AccountError("原密码错误", 401)
            account["password_hash"] = _hash_password(new_password)
            account["must_change_password"] = False
            account["auth_version"] += 1
            account["updated_at"] = _now()
            self._audit(data, account_id, account_id, "change_password")
            self._write(data)

    def login(self, username: str, password: str) -> tuple[str, dict]:
        key = username.strip().lower()
        attempts, until = self._attempts.get(key, (0, 0.0))
        if until > time.monotonic():
            raise AccountError("登录尝试过多，请稍后重试", 429)
        data = self._read()
        account = next((a for a in data["accounts"] if a["username"] == key), None)
        if not account or account["status"] != "active" or not _verify_password(password, account["password_hash"]):
            attempts += 1
            self._attempts[key] = (attempts, time.monotonic() + 60 if attempts >= 5 else 0)
            raise AccountError("用户名或密码错误", 401)
        self._attempts.pop(key, None)
        with self._write_lock():
            current = self._read()
            account = self._account(current, account["account_id"])
            if account["status"] != "active" or not _verify_password(password, account["password_hash"]):
                raise AccountError("用户名或密码错误", 401)
            account["last_login_at"] = _now()
            self._write(current)
        token = secrets.token_urlsafe(32)
        with self._local_lock:
            now = time.monotonic()
            self._sessions[token] = (account["account_id"], account["auth_version"], now, now + 8 * 3600)
        return token, public_account(account)

    def authenticate(self, token: str) -> dict:
        with self._local_lock:
            session = self._sessions.get(token)
        now = time.monotonic()
        if not session or session[2] + 30 * 60 <= now or session[3] <= now:
            raise AccountError("请先登录", 401)
        try:
            account = self._account(self._read(), session[0])
        except AccountError as exc:
            if exc.status == 404:
                raise AccountError("会话已失效，请重新登录", 401) from exc
            raise
        if account["status"] != "active" or account["auth_version"] != session[1]:
            raise AccountError("会话已失效，请重新登录", 401)
        with self._local_lock:
            if self._sessions.get(token) == session:
                self._sessions[token] = (session[0], session[1], now, session[3])
        return account

    def logout(self, token: str) -> None:
        with self._local_lock:
            self._sessions.pop(token, None)
