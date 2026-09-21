from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import IO


APP_NAME = "SmartStitch"
DATA_DIRECTORY_ENV = "SMARTSTITCH_DATA_DIRECTORY"


class ApplicationInstanceAlreadyRunningError(RuntimeError):
    """Raised before app initialization when another process owns the data directory."""


class ApplicationInstanceLock:
    """Process-scoped advisory lock guarding one writable SmartStitch data directory."""

    def __init__(self, data_directory: Path):
        self.data_directory = data_directory
        self.path = data_directory / "smartstitch.instance.lock"
        self.handle: IO[str] | None = None
        self.instance_id = uuid.uuid4().hex

    def acquire(self) -> None:
        if self.handle is not None:
            return
        self.data_directory.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip()
            handle.close()
            detail = f"（{owner}）" if owner else ""
            raise ApplicationInstanceAlreadyRunningError(
                f"SmartStitch 已在使用该数据目录运行{detail}"
            ) from exc

        payload = {
            "schema_version": 1,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "host": socket.gethostname() or "unknown",
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        handle.seek(0)
        handle.truncate()
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        self.handle = handle

    def release(self) -> None:
        handle = self.handle
        if handle is None:
            return
        self.handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


def is_frozen() -> bool:
    """Return whether SmartStitch is running from a PyInstaller bundle."""

    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def resource_root() -> Path:
    """Read-only root containing packaged frontend/config/bin resources."""

    if is_frozen():
        return Path(sys._MEIPASS).resolve()  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def application_support_root() -> Path:
    override = os.environ.get(DATA_DIRECTORY_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def writable_data_directory(application_root: Path) -> Path:
    if is_frozen():
        return application_support_root() / "data"
    return application_root / "data"


def writable_config_directory(application_root: Path) -> Path:
    if is_frozen():
        return application_support_root() / "config"
    return application_root / "config"


def seed_packaged_configs(source: Path, destination: Path) -> None:
    """Copy committed defaults once without overwriting user-edited configs."""

    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_dir() or source.resolve() == destination.resolve():
        return
    for path in source.glob("*.yaml"):
        target = destination / path.name
        if not target.exists():
            shutil.copy2(path, target)


def configure_bundled_media_tools(root: Path | None = None) -> Path | None:
    """Put packaged FFmpeg ahead of every system path when it is present."""

    binary_directory = (root or resource_root()) / "bin"
    required = (binary_directory / "ffmpeg", binary_directory / "ffprobe")
    if not all(path.is_file() and os.access(path, os.X_OK) for path in required):
        return None
    current = os.environ.get("PATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    resolved_binary_directory = str(binary_directory.resolve())
    os.environ["PATH"] = os.pathsep.join(
        [resolved_binary_directory]
        + [entry for entry in entries if entry != resolved_binary_directory]
    )
    return binary_directory
