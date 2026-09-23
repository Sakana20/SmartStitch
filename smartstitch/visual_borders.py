from __future__ import annotations

import hashlib
import json
import os
import re
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


LEGACY_GLOBAL_BORDER_DIRECTORY = Path("全局素材库/视觉去重边框")
GLOBAL_EFFECT_DIRECTORY = Path("全局素材库/视觉特效")
VISUAL_BORDER_LIBRARY_ID = "effect_1"
LEGACY_VISUAL_BORDER_LIBRARY_ID = "visual-border"
GLOBAL_BORDER_DIRECTORY = GLOBAL_EFFECT_DIRECTORY / VISUAL_BORDER_LIBRARY_ID
GLOBAL_BORDER_REGISTRY = Path(".smartstitch/visual-border-library.json")
GLOBAL_BORDER_LOCK = Path(".smartstitch/visual-border-library.lock")
GLOBAL_EFFECT_REGISTRY = Path(".smartstitch/visual-effect-libraries.json")
GLOBAL_EFFECT_LOCK = Path(".smartstitch/visual-effect-libraries.lock")
EFFECT_LIBRARY_PATTERN = re.compile(r"^effect_([1-9][0-9]*)$")
LEGACY_FLAT_EFFECT_IMPORT = "legacy-flat-visual-effects"
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
        self.effect_directory = self.root / GLOBAL_EFFECT_DIRECTORY
        self.legacy_directory = self.root / LEGACY_GLOBAL_BORDER_DIRECTORY
        self.directory = self.root / GLOBAL_BORDER_DIRECTORY
        self.registry_path = self.root / GLOBAL_BORDER_REGISTRY
        self.lock_path = self.root / GLOBAL_BORDER_LOCK
        self.effect_registry_path = self.root / GLOBAL_EFFECT_REGISTRY
        self.effect_lock_path = self.root / GLOBAL_EFFECT_LOCK
        self._lock = threading.RLock()

    def ensure_layout(self) -> None:
        self.effect_directory.mkdir(parents=True, exist_ok=True)
        if self.legacy_directory.is_dir() and not self.directory.exists():
            self.legacy_directory.replace(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        if not self.registry_path.exists():
            self._write_registry({"schema_version": 1, "revision": 0, "assets": []})
        self.effect_lock_path.touch(exist_ok=True)
        if not self.effect_registry_path.exists():
            self._write_effect_registry(
                {
                    "schema_version": 2,
                    "revision": 0,
                    "next_library_number": 2,
                    "libraries": [
                        {
                            "library_id": VISUAL_BORDER_LIBRARY_ID,
                            "name": "透明边框",
                            "enabled": True,
                            "deleted": False,
                            "created_at": _now(),
                            "assets": [],
                        }
                    ],
                }
            )

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

    @contextmanager
    def _effect_write_lock(self) -> Iterator[None]:
        with self._lock, self.effect_lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_effect_registry(self) -> dict[str, Any]:
        try:
            data = json.loads(self.effect_registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VisualBorderLibraryError(f"全局特效库注册表无法读取: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("libraries"), list):
            raise VisualBorderLibraryError("全局特效库注册表格式无效")
        data["schema_version"] = 2
        data.setdefault("revision", 0)
        legacy = next(
            (
                item for item in data["libraries"]
                if item.get("library_id") == LEGACY_VISUAL_BORDER_LIBRARY_ID
            ),
            None,
        )
        numbered_border = next(
            (
                item for item in data["libraries"]
                if item.get("library_id") == VISUAL_BORDER_LIBRARY_ID
            ),
            None,
        )
        if legacy is not None and numbered_border is None:
            legacy["library_id"] = VISUAL_BORDER_LIBRARY_ID
            numbered_border = legacy
        elif legacy is not None and numbered_border is not None:
            legacy["enabled"] = False
            legacy["deleted"] = True
            legacy.setdefault("deleted_at", _now())
        if numbered_border is None:
            data["libraries"].insert(
                0,
                {
                    "library_id": VISUAL_BORDER_LIBRARY_ID,
                    "name": "透明边框",
                    "enabled": True,
                    "deleted": False,
                    "created_at": _now(),
                    "assets": [],
                },
            )
        highest_number = max(
            (
                int(match.group(1))
                for item in data["libraries"]
                if (match := EFFECT_LIBRARY_PATTERN.fullmatch(
                    str(item.get("library_id", ""))
                ))
            ),
            default=0,
        )
        data["next_library_number"] = max(
            int(data.get("next_library_number", 1)),
            highest_number + 1,
        )
        return data

    def _write_effect_registry(self, data: dict[str, Any]) -> None:
        self.effect_registry_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".visual-effect-libraries-",
            suffix=".json.tmp",
            dir=self.effect_registry_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.effect_registry_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _canonical_effect_library_id(library_id: str) -> str:
        return (
            VISUAL_BORDER_LIBRARY_ID
            if library_id == LEGACY_VISUAL_BORDER_LIBRARY_ID
            else library_id
        )

    @staticmethod
    def _next_effect_library_id(data: dict[str, Any]) -> str:
        number = max(1, int(data.get("next_library_number", 1)))
        existing = {
            str(item.get("library_id", "")) for item in data["libraries"]
        }
        while f"effect_{number}" in existing:
            number += 1
        data["next_library_number"] = number + 1
        return f"effect_{number}"

    @staticmethod
    def _effect_asset_record(
        path: Path,
        *,
        display_name: str,
        storage_path: Path,
    ) -> dict[str, Any]:
        size = path.stat().st_size
        if size <= 0 or size > MAX_VISUAL_BORDER_BYTES:
            raise VisualBorderLibraryError("视觉特效不能为空且不能超过 500 MB")
        probe = validate_visual_border_media(path)
        digest = _sha256(path)
        extension = path.suffix.lower()
        return {
            "asset_id": f"visual-effect-{digest[:16]}",
            "display_name": display_name,
            "storage_path": str(storage_path),
            "content_hash": digest,
            "size_bytes": size,
            "created_at": _now(),
            "enabled": True,
            "deleted": False,
            "default_weight": 1.0,
            "alpha_mode": "straight",
            "media_type": "image" if extension in {".png", ".webp"} else "video",
            "probe": probe.model_dump(mode="json"),
        }

    def _import_flat_effect_library(self, data: dict[str, Any]) -> bool:
        """Move the pre-library flat effect folder into a numbered library."""
        candidates = [
            path for path in sorted(
                self.effect_directory.iterdir(), key=lambda item: item.name.casefold()
            )
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in VISUAL_BORDER_EXTENSIONS
        ]
        if not candidates:
            return False
        prepared: list[tuple[Path, dict[str, Any]]] = []
        for path in candidates:
            try:
                record = self._effect_asset_record(
                    path,
                    display_name=path.name,
                    storage_path=Path(),
                )
            except (OSError, ValueError):
                continue
            prepared.append((path, record))
        if not prepared:
            return False
        library = next(
            (
                item for item in data["libraries"]
                if item.get("legacy_import") == LEGACY_FLAT_EFFECT_IMPORT
                and not item.get("deleted")
            ),
            None,
        )
        if library is None:
            library_id = self._next_effect_library_id(data)
            library = {
                "library_id": library_id,
                "name": "视觉特效",
                "enabled": True,
                "deleted": False,
                "created_at": _now(),
                "legacy_import": LEGACY_FLAT_EFFECT_IMPORT,
                "assets": [],
            }
            data["libraries"].append(library)
        library_id = str(library["library_id"])
        directory = self.effect_directory / library_id
        directory.mkdir(parents=True, exist_ok=True)
        assets = library.setdefault("assets", [])
        known_hashes = {str(item.get("content_hash", "")) for item in assets}
        changed = False
        for source, record in prepared:
            digest = str(record["content_hash"])
            if digest in known_hashes:
                continue
            target = directory / f"{digest}{source.suffix.lower()}"
            if not target.exists():
                source.replace(target)
            record["storage_path"] = str(
                GLOBAL_EFFECT_DIRECTORY / library_id / target.name
            )
            assets.append(record)
            known_hashes.add(digest)
            changed = True
        return changed

    def _discover_effect_library_assets(self, data: dict[str, Any]) -> bool:
        """Register supported files copied directly into numbered library folders."""
        changed = False
        for library in data["libraries"]:
            library_id = str(library.get("library_id", ""))
            if (
                library.get("deleted")
                or library_id == VISUAL_BORDER_LIBRARY_ID
                or not library_id
            ):
                continue
            directory = self.effect_directory / library_id
            directory.mkdir(parents=True, exist_ok=True)
            assets = library.setdefault("assets", [])
            known_hashes = {str(item.get("content_hash", "")) for item in assets}
            known_paths = {
                (self.root / Path(str(item.get("storage_path", "")))).resolve()
                for item in assets
                if item.get("storage_path")
            }
            for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
                if (
                    not path.is_file()
                    or path.name.startswith(".")
                    or path.suffix.lower() not in VISUAL_BORDER_EXTENSIONS
                    or path.resolve() in known_paths
                ):
                    continue
                try:
                    record = self._effect_asset_record(
                        path,
                        display_name=path.name,
                        storage_path=GLOBAL_EFFECT_DIRECTORY / library_id / path.name,
                    )
                except (OSError, ValueError):
                    continue
                if record["content_hash"] in known_hashes:
                    continue
                assets.append(record)
                known_hashes.add(str(record["content_hash"]))
                known_paths.add(path.resolve())
                changed = True
        return changed

    @staticmethod
    def _check_revision(data: dict[str, Any], expected_revision: int | None) -> None:
        if expected_revision is not None and data["revision"] != expected_revision:
            raise VisualBorderLibraryConflict("全局边框库已被其他电脑修改，请刷新后重试")

    def _storage_path(self, record: dict[str, Any]) -> Path:
        stored = Path(str(record.get("storage_path", "")))
        if stored.is_absolute():
            # 旧注册表可能写入另一台 Mac 的挂载绝对路径；内容寻址文件名稳定。
            return self.directory / stored.name
        try:
            legacy_relative = stored.relative_to(LEGACY_GLOBAL_BORDER_DIRECTORY)
        except ValueError:
            pass
        else:
            return self.directory / legacy_relative
        return self.root / stored

    def _discover_unregistered(self, data: dict[str, Any]) -> bool:
        """Register valid border files copied directly into the shared directory."""
        changed = False
        for record in data["assets"]:
            stored = Path(str(record.get("storage_path", "")))
            if stored.is_absolute() or stored == Path("."):
                continue
            try:
                relative = stored.relative_to(LEGACY_GLOBAL_BORDER_DIRECTORY)
            except ValueError:
                continue
            record["storage_path"] = str(GLOBAL_BORDER_DIRECTORY / relative)
            changed = True
        existing_assets = [
            record for record in data["assets"] if self._storage_path(record).is_file()
        ]
        changed = len(existing_assets) != len(data["assets"]) or changed
        data["assets"] = existing_assets
        registered_paths = {
            self._storage_path(record).resolve()
            for record in data["assets"]
        }
        registered_hashes = {
            str(record.get("content_hash", ""))
            for record in data["assets"]
        }
        for path in sorted(
            self.directory.iterdir(), key=lambda item: item.name.casefold()
        ):
            if (
                path.name.startswith(".")
                or path.is_symlink()
                or not path.is_file()
                or path.suffix.lower() not in VISUAL_BORDER_EXTENSIONS
                or path.resolve() in registered_paths
            ):
                continue
            try:
                size = path.stat().st_size
                if size <= 0 or size > MAX_VISUAL_BORDER_BYTES:
                    continue
                probe = validate_visual_border_media(path)
                digest = _sha256(path)
            except (OSError, ValueError):
                # A file may still be copying, or may simply not be a valid transparent
                # border. Leave it untouched so a later refresh can retry it.
                continue
            if digest in registered_hashes:
                continue
            record = {
                "asset_id": f"visual-border-{digest[:16]}",
                "display_name": path.name,
                "storage_path": str(GLOBAL_BORDER_DIRECTORY / path.name),
                "content_hash": digest,
                "size_bytes": size,
                "created_at": _now(),
                "enabled": True,
                "default_weight": 1.0,
                "alpha_mode": "straight",
                "media_type": (
                    "image" if path.suffix.lower() in {".png", ".webp"} else "video"
                ),
                "probe": probe.model_dump(mode="json"),
            }
            data["assets"].append(record)
            registered_paths.add(path.resolve())
            registered_hashes.add(digest)
            changed = True
        return changed

    def _snapshot(self, data: dict[str, Any]) -> dict[str, Any]:
        assets = []
        for asset in data["assets"]:
            record = dict(asset)
            record["storage_path"] = str(
                GLOBAL_BORDER_DIRECTORY / self._storage_path(record).name
            )
            assets.append(record)
        return {
            "directory": str(self.directory),
            "revision": data["revision"],
            "assets": assets,
        }

    def list(self) -> dict[str, Any]:
        self.ensure_layout()
        with self._write_lock():
            data = self._read_registry()
            if self._discover_unregistered(data):
                data["revision"] += 1
                self._write_registry(data)
        return self._snapshot(data)

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
                return {
                    **self._snapshot(data),
                    "asset": dict(existing),
                    "deduplicated": True,
                }
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
            return {
                **self._snapshot(data),
                "asset": dict(record),
                "deduplicated": False,
            }

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
            return {**self._snapshot(data), "asset": dict(record)}

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

    def _effect_snapshot(self, data: dict[str, Any]) -> dict[str, Any]:
        legacy = self.list()
        libraries: list[dict[str, Any]] = []
        for raw in data["libraries"]:
            if raw.get("deleted"):
                continue
            record = dict(raw)
            if record["library_id"] == VISUAL_BORDER_LIBRARY_ID:
                record["directory"] = str(self.directory)
                record["assets"] = legacy["assets"]
            else:
                record["directory"] = str(
                    self.effect_directory / record["library_id"]
                )
                record["assets"] = [
                    dict(asset) for asset in record.get("assets", [])
                    if not asset.get("deleted")
                ]
            libraries.append(record)
        return {
            "directory": str(self.effect_directory),
            "revision": data["revision"],
            "libraries": libraries,
        }

    def list_effect_libraries(self) -> dict[str, Any]:
        self.ensure_layout()
        with self._effect_write_lock():
            data = self._read_effect_registry()
            changed = self._import_flat_effect_library(data)
            changed = self._discover_effect_library_assets(data) or changed
            if changed:
                data["revision"] += 1
            self._write_effect_registry(data)
        return self._effect_snapshot(data)

    def effect_library_directory(self, library_id: str) -> Path:
        """Return the stable numbered directory for a visible effect library."""
        self.ensure_layout()
        canonical_id = self._canonical_effect_library_id(library_id)
        with self._effect_write_lock():
            data = self._read_effect_registry()
            self._effect_library(data, canonical_id)
            directory = self.effect_directory / canonical_id
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _effect_library(
        data: dict[str, Any], library_id: str, *, include_deleted: bool = False
    ) -> dict[str, Any]:
        library_id = VisualBorderLibrary._canonical_effect_library_id(library_id)
        record = next(
            (
                item
                for item in data["libraries"]
                if item.get("library_id") == library_id
                and (include_deleted or not item.get("deleted"))
            ),
            None,
        )
        if record is None:
            raise VisualBorderLibraryError("全局特效库不存在")
        return record

    def create_effect_library(
        self, name: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self.ensure_layout()
        clean_name = name.strip()
        if not clean_name:
            raise VisualBorderLibraryError("特效库名称不能为空")
        with self._effect_write_lock():
            data = self._read_effect_registry()
            self._check_revision(data, expected_revision)
            if any(
                not item.get("deleted")
                and str(item.get("name", "")).casefold() == clean_name.casefold()
                for item in data["libraries"]
            ):
                raise VisualBorderLibraryError("已存在同名全局特效库")
            library_id = self._next_effect_library_id(data)
            record = {
                "library_id": library_id,
                "name": clean_name,
                "enabled": True,
                "deleted": False,
                "created_at": _now(),
                "assets": [],
            }
            (self.effect_directory / library_id).mkdir(parents=True, exist_ok=False)
            data["libraries"].append(record)
            data["revision"] += 1
            self._write_effect_registry(data)
        return {**self._effect_snapshot(data), "library": dict(record)}

    def update_effect_library(
        self,
        library_id: str,
        *,
        expected_revision: int,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        self.ensure_layout()
        library_id = self._canonical_effect_library_id(library_id)
        with self._effect_write_lock():
            data = self._read_effect_registry()
            self._check_revision(data, expected_revision)
            record = self._effect_library(data, library_id)
            if updates.get("name") is not None:
                name = str(updates["name"]).strip()
                if not name:
                    raise VisualBorderLibraryError("特效库名称不能为空")
                if any(
                    item is not record
                    and not item.get("deleted")
                    and str(item.get("name", "")).casefold() == name.casefold()
                    for item in data["libraries"]
                ):
                    raise VisualBorderLibraryError("已存在同名全局特效库")
                record["name"] = name
            if updates.get("enabled") is not None:
                record["enabled"] = bool(updates["enabled"])
            data["revision"] += 1
            self._write_effect_registry(data)
        return {**self._effect_snapshot(data), "library": dict(record)}

    def delete_effect_library(
        self, library_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self.ensure_layout()
        library_id = self._canonical_effect_library_id(library_id)
        with self._effect_write_lock():
            data = self._read_effect_registry()
            self._check_revision(data, expected_revision)
            record = self._effect_library(data, library_id)
            record["enabled"] = False
            record["deleted"] = True
            record["deleted_at"] = _now()
            data["revision"] += 1
            self._write_effect_registry(data)
        return self._effect_snapshot(data)

    def upload_effect_asset(
        self,
        library_id: str,
        filename: str,
        uploaded_path: Path,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.ensure_layout()
        library_id = self._canonical_effect_library_id(library_id)
        clean_name = Path(filename.strip()).name
        if not clean_name or clean_name != filename.strip():
            raise VisualBorderLibraryError("特效文件名无效")
        extension = Path(clean_name).suffix.lower()
        if extension not in VISUAL_BORDER_EXTENSIONS:
            raise VisualBorderLibraryError("特效仅支持 MOV、PNG 或 WebP")
        size = uploaded_path.stat().st_size
        if size <= 0 or size > MAX_VISUAL_BORDER_BYTES:
            raise VisualBorderLibraryError("视觉特效不能为空且不能超过 500 MB")
        probe = validate_visual_border_media(uploaded_path)
        digest = _sha256(uploaded_path)
        with self._effect_write_lock():
            data = self._read_effect_registry()
            self._check_revision(data, expected_revision)
            library = self._effect_library(data, library_id)
            if library_id == VISUAL_BORDER_LIBRARY_ID:
                legacy_revision = self.list()["revision"]
                legacy_result = self.upload(
                    clean_name, uploaded_path, legacy_revision
                )
                asset = legacy_result["asset"]
                deduplicated = legacy_result["deduplicated"]
            else:
                assets = library.setdefault("assets", [])
                existing = next(
                    (
                        item for item in assets
                        if item.get("content_hash") == digest and not item.get("deleted")
                    ),
                    None,
                )
                if existing is not None:
                    return {
                        **self._effect_snapshot(data),
                        "asset": dict(existing),
                        "deduplicated": True,
                    }
                directory = self.effect_directory / library_id
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / f"{digest}{extension}"
                staged = directory / f".{digest}.upload{extension}"
                shutil.copyfile(uploaded_path, staged)
                try:
                    if not target.exists():
                        staged.replace(target)
                finally:
                    staged.unlink(missing_ok=True)
                asset = {
                    "asset_id": f"visual-effect-{digest[:16]}",
                    "display_name": clean_name,
                    "storage_path": str(GLOBAL_EFFECT_DIRECTORY / library_id / target.name),
                    "content_hash": digest,
                    "size_bytes": size,
                    "created_at": _now(),
                    "enabled": True,
                    "deleted": False,
                    "default_weight": 1.0,
                    "alpha_mode": "straight",
                    "media_type": "image" if extension in {".png", ".webp"} else "video",
                    "probe": probe.model_dump(mode="json"),
                }
                assets.append(asset)
                deduplicated = False
            data["revision"] += 1
            self._write_effect_registry(data)
        return {
            **self._effect_snapshot(data),
            "asset": dict(asset),
            "deduplicated": deduplicated,
        }

    def update_effect_asset(
        self,
        library_id: str,
        asset_id: str,
        *,
        expected_revision: int,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        self.ensure_layout()
        library_id = self._canonical_effect_library_id(library_id)
        with self._effect_write_lock():
            data = self._read_effect_registry()
            self._check_revision(data, expected_revision)
            library = self._effect_library(data, library_id)
            if library_id == VISUAL_BORDER_LIBRARY_ID:
                legacy_revision = self.list()["revision"]
                result = self.update(
                    asset_id,
                    expected_revision=legacy_revision,
                    updates=updates,
                )
                asset = result["asset"]
            else:
                asset = next(
                    (
                        item for item in library.get("assets", [])
                        if item.get("asset_id") == asset_id and not item.get("deleted")
                    ),
                    None,
                )
                if asset is None:
                    raise VisualBorderLibraryError("全局特效素材不存在")
                asset.update(
                    {key: value for key, value in updates.items() if value is not None}
                )
            data["revision"] += 1
            self._write_effect_registry(data)
        return {**self._effect_snapshot(data), "asset": dict(asset)}

    def delete_effect_asset(
        self, library_id: str, asset_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        library_id = self._canonical_effect_library_id(library_id)
        if library_id == VISUAL_BORDER_LIBRARY_ID:
            return self.update_effect_asset(
                library_id,
                asset_id,
                expected_revision=expected_revision,
                updates={"enabled": False},
            )
        self.ensure_layout()
        with self._effect_write_lock():
            data = self._read_effect_registry()
            self._check_revision(data, expected_revision)
            library = self._effect_library(data, library_id)
            asset = next(
                (
                    item for item in library.get("assets", [])
                    if item.get("asset_id") == asset_id and not item.get("deleted")
                ),
                None,
            )
            if asset is None:
                raise VisualBorderLibraryError("全局特效素材不存在")
            asset["enabled"] = False
            asset["deleted"] = True
            asset["deleted_at"] = _now()
            data["revision"] += 1
            self._write_effect_registry(data)
        return self._effect_snapshot(data)

    def assets_for_effect_layers(self, config: AppConfig) -> dict[str, list[Asset]]:
        snapshot = self.list_effect_libraries()
        libraries = {
            item["library_id"]: item for item in snapshot["libraries"]
        }
        result: dict[str, list[Asset]] = {}
        for layer in config.visual_dedup.resolved_effect_layers():
            if layer.type != "overlay" or not layer.enabled:
                continue
            category = f"visual_effect:{layer.layer_id}"
            library_id = self._canonical_effect_library_id(layer.library_id)
            library = libraries.get(library_id)
            if library is None or not library.get("enabled", True):
                result[category] = [
                    Asset(
                        id=f"missing-{layer.layer_id}",
                        category=category,
                        path="",
                        name=layer.name,
                        media_type="image",
                        exists=False,
                        valid=False,
                        error=f"全局特效库“{layer.library_id}”不存在或已停用",
                    )
                ]
                continue
            allowed = set(layer.enabled_asset_ids)
            assets: list[Asset] = []
            for record in library.get("assets", []):
                if not record.get("enabled", True):
                    continue
                if allowed and record["asset_id"] not in allowed:
                    continue
                stored = Path(str(record.get("storage_path", "")))
                if stored.is_absolute():
                    path = stored
                elif library_id == VISUAL_BORDER_LIBRARY_ID:
                    path = self._storage_path(record)
                else:
                    path = self.root / stored
                exists = path.is_file()
                probe = MediaProbe.model_validate(record["probe"])
                error = None
                if not exists:
                    error = "全局特效文件不存在"
                elif layer.scale_mode == "exact" and (
                    probe.width != config.output.width
                    or probe.height != config.output.height
                ):
                    error = (
                        f"特效尺寸 {probe.width}x{probe.height} 与输出画布 "
                        f"{config.output.width}x{config.output.height} 不一致"
                    )
                assets.append(
                    Asset(
                        id=record["asset_id"],
                        category=category,
                        path=str(path),
                        name=record["display_name"],
                        media_type=record["media_type"],
                        weight=layer.weights.get(
                            record["asset_id"], record.get("default_weight", 1.0)
                        ),
                        exists=exists,
                        valid=error is None,
                        error=error,
                        size_bytes=record.get("size_bytes"),
                        modified_at=path.stat().st_mtime if exists else None,
                        content_hash=record.get("content_hash"),
                        alpha_mode=record.get("alpha_mode", "straight"),
                        probe=probe,
                    )
                )
            result[category] = assets
        return result
