"""Read NAS release metadata and open a locally verified disk image."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any


_VERSION = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_FILENAME = re.compile(r"^SmartStitch-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-macOS-arm64\.dmg$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
CHUNK_SIZE = 1024 * 1024


class UpdateCheckError(RuntimeError):
    """The update source or package cannot be used."""


def is_newer_version(candidate: str, current: str) -> bool:
    def key(value: str) -> tuple[int, ...]:
        match = _VERSION.fullmatch(value)
        if match is None:
            raise ValueError(f"不支持的版本号：{value}")
        return tuple(map(int, match.groups()))

    return key(candidate) > key(current)


def read_manifest(updates: Path) -> dict[str, Any] | None:
    path = updates / "latest.json"
    if path.is_symlink():
        raise UpdateCheckError("NAS 更新清单无效")
    if not path.exists():
        return None
    try:
        if path.is_symlink() or path.stat().st_size > 16 * 1024:
            raise UpdateCheckError("NAS 更新清单无效")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise UpdateCheckError("无法读取 NAS 更新清单") from exc
    if not isinstance(data, dict):
        raise UpdateCheckError("NAS 更新清单无效")
    version, filename = data.get("version"), data.get("filename")
    size, digest = data.get("size_bytes"), data.get("sha256")
    if (
        data.get("schema_version") != 1
        or data.get("platform") != "macos"
        or data.get("architecture") != "arm64"
        or not isinstance(version, str)
        or not _VERSION.fullmatch(version)
        or not isinstance(filename, str)
        or not _FILENAME.fullmatch(filename)
        or filename != f"SmartStitch-v{version.removeprefix('v')}-macOS-arm64.dmg"
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
    ):
        raise UpdateCheckError("NAS 更新清单无效")
    return data


def file_matches(path: Path, manifest: dict[str, Any]) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != manifest["size_bytes"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest() == manifest["sha256"]


class NASUpdateChecker:
    def __init__(self, config_directory: Path | None, cache_directory: Path, *, allow_open: bool = True):
        self.config_directory = config_directory
        self.cache_directory = cache_directory
        self.allow_open = allow_open
        self._available: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def check(self, current_version: str) -> dict[str, object]:
        self._available = None
        root = self.config_directory
        unavailable = {
            "source": "nas", "status": "nas_unavailable",
            "message": "未连接 NAS，请连接 NAS 后检查更新",
            "current_version": current_version, "update_available": False,
        }
        if root is None or not root.is_dir():
            self._available = None
            return unavailable
        try:
            updates = root / "updates"
            if not updates.exists():
                self._available = None
                return {"source": "nas", "status": "not_published", "current_version": current_version, "update_available": False}
            if updates.is_symlink() or not updates.is_dir():
                raise UpdateCheckError("无法读取 NAS 更新目录，请检查权限")
            manifest = read_manifest(updates)
            if manifest is None:
                self._available = None
                return {"source": "nas", "status": "not_published", "current_version": current_version, "update_available": False}
            source = updates / "releases" / manifest["filename"]
            if (updates / "releases").is_symlink() or source.is_symlink() or not source.is_file():
                raise UpdateCheckError("NAS 更新包不存在或不可读取")
            if source.stat().st_size != manifest["size_bytes"]:
                raise UpdateCheckError("NAS 更新包大小与清单不符")
            newer = is_newer_version(manifest["version"], current_version)
            self._available = manifest if newer else None
            return {
                "source": "nas", "status": "available" if newer else "up_to_date",
                "current_version": current_version, "latest_version": manifest["version"].removeprefix("v"),
                "update_available": newer, "release_name": f"SmartStitch v{manifest['version']}",
                "published_at": manifest.get("published_at"), "notes": str(manifest.get("notes") or ""),
            }
        except OSError as exc:
            self._available = None
            if not root.is_dir():
                return unavailable
            raise UpdateCheckError("无法读取 NAS 更新目录，请检查权限") from exc

    def open_latest_release(self) -> dict[str, object]:
        with self._lock:
            if not self.allow_open:
                raise UpdateCheckError("源码开发模式不支持安装更新")
            manifest = self._available
            if manifest is None:
                raise UpdateCheckError("请先检查 NAS 更新")
            root = self.config_directory
            if root is None or not root.is_dir():
                raise UpdateCheckError("未连接 NAS，请连接 NAS 后检查更新")
            source_root = root / "updates"
            if source_root.is_symlink() or (source_root / "releases").is_symlink():
                raise UpdateCheckError("NAS 更新目录无效")
            if read_manifest(source_root) != manifest:
                raise UpdateCheckError("NAS 更新信息已变化，请重新检查更新")
            source = source_root / "releases" / manifest["filename"]
            if source.is_symlink() or not source.is_file():
                raise UpdateCheckError("NAS 更新包不存在或不可读取")
            try:
                self.cache_directory.mkdir(parents=True, exist_ok=True)
                target = self.cache_directory / manifest["filename"]
                if not file_matches(target, manifest):
                    temporary = self.cache_directory / f".{manifest['filename']}.{uuid.uuid4().hex}.part"
                    try:
                        digest = hashlib.sha256()
                        total = 0
                        with source.open("rb") as reader, temporary.open("xb") as writer:
                            while chunk := reader.read(CHUNK_SIZE):
                                writer.write(chunk)
                                digest.update(chunk)
                                total += len(chunk)
                                if total > manifest["size_bytes"]:
                                    raise UpdateCheckError("NAS 更新包大小与清单不符")
                            writer.flush()
                            os.fsync(writer.fileno())
                        if total != manifest["size_bytes"] or digest.hexdigest() != manifest["sha256"]:
                            raise UpdateCheckError("NAS 更新包校验失败，请重试")
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise UpdateCheckError("无法复制 NAS 更新包，请检查连接和本机空间") from exc
            if sys.platform != "darwin":
                raise UpdateCheckError("只支持在 macOS 上打开更新安装包")
            try:
                subprocess.run(["open", str(target)], check=True, timeout=15)
            except (OSError, subprocess.SubprocessError) as exc:
                raise UpdateCheckError("无法打开更新安装包") from exc
            for old in self.cache_directory.glob("SmartStitch-v*-macOS-arm64.dmg"):
                if old != target and old.is_file() and not old.is_symlink():
                    try:
                        old.unlink()
                    except OSError:
                        pass
            return {"opened": True, "version": manifest["version"]}
