"""Local, temporary password gate for the future cluster control tool."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from threading import Lock

from pydantic import BaseModel, SecretStr


_SALT = b"smartstitch-cluster-control-v1"
_ITERATIONS = 200_000
_DEFAULT_PASSWORD_HASH = bytes.fromhex(
    "7e553ab9bd5643c1ae7683a598efd5938e4afc6c064ef54cb1bcf04cc128602b"
)
SESSION_SECONDS = 15 * 60


class ClusterUnlockRequest(BaseModel):
    password: SecretStr


class ClusterAccess:
    """Issue short lived process local tokens after checking the cluster password."""

    def __init__(self) -> None:
        override = os.environ.get("SMARTSTITCH_CLUSTER_PASSWORD")
        if override == "":
            raise ValueError("SMARTSTITCH_CLUSTER_PASSWORD 不能设为空密码")
        self._password_hash = (
            self._digest(override) if override is not None else _DEFAULT_PASSWORD_HASH
        )
        self._sessions: dict[str, float] = {}
        self._lock = Lock()

    @staticmethod
    def _digest(password: str) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _SALT, _ITERATIONS
        )

    def unlock(self, password: str) -> str | None:
        if not hmac.compare_digest(self._digest(password), self._password_hash):
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.monotonic() + SESSION_SECONDS
        return token

    def authorized(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self._sessions.get(token, 0)
            if expiry <= time.monotonic():
                self._sessions.pop(token, None)
                return False
            return True

    def lock(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)
