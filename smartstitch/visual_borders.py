from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .models import AppConfig, Asset, MediaProbe
from .scanner import validate_visual_border_media

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


GLOBAL_BORDER_DIRECTORY = Path("全局素材库/视觉去重边框")
GLOBAL_BORDER_REGISTRY = Path(".smartstitch/visual-border-library.json")
GLOBAL_BORDER_LOCK = Path(".smartstitch/visual-border-library.lock")
VISUAL_BORDER_EXTENSIONS = {".mov", ".png", ".webp"}
MAX_VISUAL_BORDER_BYTES = 500 * 1024 * 1024


class VisualBorderLibraryError(ValueError):
    pass


class VisualBorderLibraryConflict(VisualBorderLibraryError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class VisualBorderLibrary:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.directory = self.root / GLOBAL_BORDER_DIRECTORY
        self.registry_path = self.root / GLOBAL_BORDER_REGISTRY
        self.lock_path = self.root / GLOBAL_BORDER_LOCK
        self._lock = threading.RLock()

    def ensure_layout(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        if not self.registry_path.exists():
            self._write_registry({"schema_version": 1, "revision": 0, "assets": []})

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        with self._lock, self.lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_registry(self) -> dict[str, Any]:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VisualBorderLibraryError(f"全局边框库注册表无法读取: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
            raise VisualBorderLibraryError("全局边框库注册表格式无效")
        data.setdefault("schema_version", 1)
        data.setdefault("revision", 0)
        return data

    def _write_registry(self, data: dict[str, Any]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".visual-border-library-",
            suffix=".json.tmp",
            dir=self.registry_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.registry_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _check_revision(data: dict[str, Any], expected_revision: int | None) -> None:
        if expected_revision is not None and data["revision"] != expected_revision:
            raise VisualBorderLibraryConflict("全局边框库已被其他电脑修改，请刷新后重试")

    def _storage_path(self, record: dict[str, Any]) -> Path:
        stored = Path(str(record.get("storage_path", "")))
        if stored.is_absolute():
            # 旧注册表可能写入另一台 Mac 的挂载绝对路径；内容寻址文件名稳定。
            return self.directory / stored.name
        return self.root / stored

    def list(self) -> dict[str, Any]:
        self.ensure_layout()
        with self._lock:
            data = self._read_registry()
        return {
            "directory": str(self.directory),
            "revision": data["revision"],
            "assets": [dict(asset) for asset in data["assets"]],
        }

    def upload(
        self,
        filename: str,
        uploaded_path: Path,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        self.ensure_layout()
        clean_name = Path(filename.strip()).name
        if not clean_name or clean_name != filename.strip():
            raise VisualBorderLibraryError("边框文件名无效")
        extension = Path(clean_name).suffix.lower()
        if extension not in VISUAL_BORDER_EXTENSIONS:
            raise VisualBorderLibraryError("边框仅支持 MOV、PNG 或 WebP")
        size = uploaded_path.stat().st_size
        if size <= 0 or size > MAX_VISUAL_BORDER_BYTES:
            raise VisualBorderLibraryError("视觉去重边框不能为空且不能超过 500 MB")
        probe = validate_visual_border_media(uploaded_path)
        digest = _sha256(uploaded_path)
        asset_id = f"visual-border-{digest[:16]}"
        target = self.directory / f"{digest}{extension}"

        with self._write_lock():
            data = self._read_registry()
            self._check_revision(data, expected_revision)
            existing = next(
                (asset for asset in data["assets"] if asset["content_hash"] == digest),
                None,
            )
            if existing is not None:
                return {**self.list(), "asset": dict(existing), "deduplicated": True}
            staged = self.directory / f".{digest}.upload{extension}"
            shutil.copyfile(uploaded_path, staged)
            try:
                if not target.exists():
                    staged.replace(target)
            finally:
                staged.unlink(missing_ok=True)
            record = {
                "asset_id": asset_id,
                "display_name": clean_name,
                "storage_path": str(GLOBAL_BORDER_DIRECTORY / target.name),
                "content_hash": digest,
                "size_bytes": size,
                "created_at": _now(),
                "enabled": True,
                "default_weight": 1.0,
                "alpha_mode": "straight",
                "media_type": "image" if extension in {".png", ".webp"} else "video",
                "probe": probe.model_dump(mode="json"),
            }
            data["assets"].append(record)
            data["revision"] += 1
            self._write_registry(data)
            return {**self.list(), "asset": dict(record), "deduplicated": False}

    def update(
        self,
        asset_id: str,
        *,
        expected_revision: int,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        self.ensure_layout()
        with self._write_lock():
            data = self._read_registry()
            self._check_revision(data, expected_revision)
            record = next(
                (asset for asset in data["assets"] if asset["asset_id"] == asset_id),
                None,
            )
            if record is None:
                raise VisualBorderLibraryError("全局边框不存在")
            record.update({key: value for key, value in updates.items() if value is not None})
            data["revision"] += 1
            self._write_registry(data)
            return {**self.list(), "asset": dict(record)}

    def assets_for_config(self, config: AppConfig) -> list[Asset]:
        settings = config.visual_dedup.border_overlay
        records = self.list()["assets"]
        allowed = set(settings.enabled_asset_ids)
        result: list[Asset] = []
        for record in records:
            if not record.get("enabled", True):
                continue
            if allowed and record["asset_id"] not in allowed:
                continue
            path = self._storage_path(record)
            exists = path.is_file()
            probe = MediaProbe.model_validate(record["probe"])
            error = None
            if not exists:
                error = "全局边框文件不存在"
            elif settings.scale_mode == "exact" and (
                probe.width != config.output.width or probe.height != config.output.height
            ):
                error = (
                    f"边框尺寸 {probe.width}x{probe.height} 与输出画布 "
                    f"{config.output.width}x{config.output.height} 不一致"
                )
            weight = settings.weights.get(
                record["asset_id"], record.get("default_weight", 1.0)
            )
            result.append(
                Asset(
                    id=record["asset_id"],
                    category="visual_border",
                    path=str(path),
                    name=record["display_name"],
                    media_type=record["media_type"],
                    weight=weight,
                    exists=exists,
                    valid=error is None,
                    error=error,
                    size_bytes=record["size_bytes"],
                    modified_at=path.stat().st_mtime if exists else None,
                    content_hash=record["content_hash"],
                    alpha_mode=record.get("alpha_mode", "straight"),
                    probe=probe,
                )
            )
        return result
