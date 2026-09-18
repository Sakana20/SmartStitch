from __future__ import annotations

import base64
import binascii
import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ConfigError, ConfigStore
from .models import (
    AddBenefitRequest,
    AddPoolRequest,
    AppConfig,
    CreateLibraryRequest,
    DeletePoolRequest,
    LibraryPreflightRequest,
    MAX_GENERIC_POOLS,
    ReplaceOverlayImageRequest,
    ReorderTimelineRequest,
    SourceGroupConfig,
    SourceMode,
    UpdatePoolRequest,
    is_benefit_category,
    is_pool_category,
    resolve_directory,
)


LAYOUT_VERSION = 1
GENERIC_LAYOUT_VERSION = 2
MARKER_PATH = Path(".smartstitch/library.json")
STANDARD_DIRECTORIES = (
    Path("原始视频"),
    Path("切片素材/前贴"),
    Path("切片素材/引子"),
    Path("切片素材/利益点/1"),
    Path("切片素材/结尾"),
    Path("切片素材/尾帧"),
    Path("切片素材/未归类"),
    Path("风险提示语图片"),
    Path("成片输出"),
    Path("工作记录/切片清单"),
    Path("工作记录/生成清单"),
    Path(".smartstitch"),
)
GENERIC_DIRECTORIES = (
    Path("原始视频"),
    Path("视频库"),
    Path("未归类"),
    Path("风险提示语图片"),
    Path("成片输出"),
    Path("工作记录/切片清单"),
    Path("工作记录/生成清单"),
    Path(".smartstitch"),
)
INVALID_FOLDER_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00]')
OVERLAY_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_OVERLAY_IMAGE_BYTES = 20 * 1024 * 1024
IGNORABLE_SELECTED_ROOT_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


class LibraryError(ValueError):
    pass


class LibraryConflictError(LibraryError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_folder_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise LibraryError("视频库文件夹名不能为空")
    if name in {".", ".."} or INVALID_FOLDER_CHARACTERS.search(name):
        raise LibraryError("视频库文件夹名包含非法字符")
    return name


def _library_root(parent: Path, folder_name: str) -> tuple[Path, bool]:
    use_selected_directory = parent.name.casefold() == folder_name.casefold()
    return (parent if use_selected_directory else parent / folder_name), use_selected_directory


def open_directory_in_file_manager(path: Path) -> dict[str, Any]:
    directory = path.expanduser().resolve()
    if not directory.exists() or not directory.is_dir():
        raise LibraryError(f"视频库文件夹不存在: {directory}")
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(
                ["open", str(directory)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            manager = "Finder"
        elif system == "Windows":
            startfile = getattr(os, "startfile", None)
            if callable(startfile):
                startfile(str(directory))
            else:
                subprocess.Popen(
                    ["explorer", str(directory)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            manager = "资源管理器"
        elif system == "Linux":
            subprocess.Popen(
                ["xdg-open", str(directory)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            manager = "文件管理器"
        else:
            raise LibraryError("当前系统不支持自动打开视频库文件夹")
    except OSError as exc:
        raise LibraryError(f"无法打开视频库文件夹: {exc}") from exc
    return {"ok": True, "path": str(directory), "manager": manager}


def _within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _marker_payload(request: CreateLibraryRequest) -> dict[str, Any]:
    if request.workflow_type == "generic":
        return {
            "layout_version": GENERIC_LAYOUT_VERSION,
            "workflow_type": "generic",
            "library_id": str(uuid.uuid4()),
            "created_for_config_id": request.new_id,
            "display_name": request.new_name.strip(),
            "created_at": _now(),
            "create_request_id": request.client_request_id,
            "next_pool_number": 1,
            "recent_requests": {},
            "paths": {
                "originals": "原始视频",
                "pools": "视频库",
                "unclassified": "未归类",
                "overlays": "风险提示语图片",
                "outputs": "成片输出",
                "records": "工作记录",
            },
        }
    return {
        "layout_version": LAYOUT_VERSION,
        "library_id": str(uuid.uuid4()),
        "created_for_config_id": request.new_id,
        "display_name": request.new_name.strip(),
        "created_at": _now(),
        "create_request_id": request.client_request_id,
        "next_benefit_number": 2,
        "recent_requests": {},
        "paths": {
            "originals": "原始视频",
            "slices": "切片素材",
            "benefits": "切片素材/利益点",
            "overlays": "风险提示语图片",
            "outputs": "成片输出",
            "records": "工作记录",
        },
    }


def _standard_config(request: CreateLibraryRequest, root: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "schema_version": 2,
            "id": request.new_id,
            "name": request.new_name.strip(),
            "enabled": True,
            "description": "SmartStitch 标准视频库",
            "source_root": str(root),
            "timeline": ["pre_roll", "hook", "benefit_1", "ending", "end_card"],
            "sources": {
                "pre_roll": {
                    "mode": "optional",
                    "directory": "切片素材/前贴",
                    "extensions": [".mp4", ".mov", ".mkv"],
                },
                "hook": {"mode": "required", "directory": "切片素材/引子"},
                "benefit_1": {
                    "mode": "required",
                    "directory": "切片素材/利益点/1",
                },
                "ending": {"mode": "required", "directory": "切片素材/结尾"},
                "end_card": {
                    "mode": "optional",
                    "directory": "切片素材/尾帧",
                    "extensions": [".png", ".jpg", ".jpeg", ".webp", ".mp4"],
                },
            },
            # 标准库在此目录只有一张图片时由扫描器自动识别。
            "benefit_overlays": {"mode": "optional", "file": ""},
            "output": {"directory": str(root / "成片输出")},
        }
    )


def _generic_config(request: CreateLibraryRequest, root: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": request.new_id,
            "name": request.new_name.strip(),
            "enabled": True,
            "description": "SmartStitch 通用视频库",
            "source_root": str(root),
            "timeline": [],
            "sources": {},
            "benefit_overlays": {
                "mode": "optional",
                "file": "",
                "timing": {"scope": "full"},
            },
            "output": {"directory": str(root / "成片输出")},
        }
    )


def _directories_for(workflow_type: str) -> tuple[Path, ...]:
    return GENERIC_DIRECTORIES if workflow_type == "generic" else STANDARD_DIRECTORIES


class LibraryService:
    def __init__(self, config_store: ConfigStore):
        self.config_store = config_store
        self._lock = threading.RLock()

    def preflight(self, request: LibraryPreflightRequest) -> dict[str, Any]:
        name = _safe_folder_name(request.folder_name)
        parent = Path(request.parent_directory).expanduser()
        if not parent.is_absolute():
            raise LibraryError("保存位置必须是绝对路径")
        try:
            parent = parent.resolve(strict=True)
        except FileNotFoundError as exc:
            raise LibraryError("保存位置不存在，可能是外接卷未挂载") from exc
        if not parent.is_dir():
            raise LibraryError("保存位置不是文件夹")
        if not os.access(parent, os.W_OK | os.X_OK):
            raise LibraryError(f"保存位置不可写: {parent}")
        root, use_selected_directory = _library_root(parent, name)
        if not use_selected_directory and root.parent != parent:
            raise LibraryError("视频库必须是保存位置的直接子目录")
        if use_selected_directory:
            occupied = sorted(
                item.name
                for item in root.iterdir()
                if item.name not in IGNORABLE_SELECTED_ROOT_NAMES
            )
            if occupied:
                preview = "、".join(occupied[:3])
                suffix = "等" if len(occupied) > 3 else ""
                raise LibraryConflictError(
                    f"所选文件夹将直接作为视频库，但其中已有内容: {preview}{suffix}"
                )
        elif root.exists() or root.is_symlink():
            raise LibraryConflictError(f"同名视频库已存在: {root}")
        return {
            "ok": True,
            "workflow_type": request.workflow_type,
            "parent_directory": str(parent),
            "folder_name": name,
            "root_path": str(root),
            "uses_selected_directory": use_selected_directory,
            "directories": [
                str(path) for path in _directories_for(request.workflow_type)
            ],
        }

    def create(self, request: CreateLibraryRequest) -> dict[str, Any]:
        with self._lock, self.config_store.lock:
            name = _safe_folder_name(request.folder_name)
            if not request.new_name.strip():
                raise LibraryError("配置名称不能为空")
            parent = Path(request.parent_directory).expanduser()
            if not parent.is_absolute():
                raise LibraryError("保存位置必须是绝对路径")
            parent = parent.resolve()
            root, use_selected_directory = _library_root(parent, name)

            existing = self._idempotent_create_result(root, request)
            if existing is not None:
                return existing

            preflight = self.preflight(
                LibraryPreflightRequest(
                    parent_directory=str(parent),
                    folder_name=name,
                    workflow_type=request.workflow_type,
                )
            )
            if self.config_store.path_for(request.new_id).exists():
                raise LibraryConflictError(f"配置已存在: {request.new_id}")

            temporary = (
                root
                if use_selected_directory
                else Path(tempfile.mkdtemp(prefix=".smartstitch-create-", dir=parent))
            )
            committed = use_selected_directory
            try:
                marker = _marker_payload(request)
                (temporary / MARKER_PATH.parent).mkdir(parents=True, exist_ok=True)
                _write_json_atomic(temporary / MARKER_PATH, marker)
                for relative in _directories_for(request.workflow_type):
                    (temporary / relative).mkdir(parents=True, exist_ok=True)
                config = (
                    _generic_config(request, root)
                    if request.workflow_type == "generic"
                    else _standard_config(request, root)
                )
                if not preflight["uses_selected_directory"]:
                    if root.exists() or root.is_symlink():
                        raise LibraryConflictError(f"同名视频库已存在: {root}")
                    temporary.rename(root)
                    committed = True
                self.config_store.create_config(config)
            except Exception:
                cleanup_target = root if committed else temporary
                self._cleanup_owned_tree(cleanup_target, request.client_request_id)
                raise

            return self._create_result(root, config, marker, idempotent=False)

    def inspect_by_config(self, config_id: str) -> dict[str, Any]:
        config = self.config_store.load(config_id)
        root = Path(config.source_root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        marker_path = root / MARKER_PATH
        if not marker_path.is_file():
            return {
                "managed": False,
                "health": "unmanaged",
                "root_path": str(root),
                "missing_directories": [],
            }
        try:
            marker = self._load_marker(root)
            overlay_layout_upgraded = False
            if config.workflow_type == "generic":
                overlay_layout_upgraded = self._ensure_generic_overlay_layout(
                    root, marker
                )
                if (
                    overlay_layout_upgraded
                    and config.benefit_overlays.mode == SourceMode.DISABLED
                ):
                    config = config.model_copy(deep=True)
                    config.benefit_overlays.mode = SourceMode.OPTIONAL
                    config.benefit_overlays.timing.scope = "full"
                    self.config_store.save_config(config_id, config)
        except (OSError, json.JSONDecodeError, LibraryError) as exc:
            return {
                "managed": True,
                "health": "invalid_marker",
                "root_path": str(root),
                "error": str(exc),
                "missing_directories": [],
            }
        required = [
            root / relative
            for relative in _directories_for(config.workflow_type)
        ]
        required.extend(
            resolve_directory(config, group.directory)
            for group in config.sources.values()
            if config.workflow_type == "generic"
        )
        missing = [str(path) for path in required if not path.is_dir()]
        return {
            "managed": True,
            "health": "healthy" if not missing else "needs_repair",
            "root_path": str(root.resolve()),
            "library_id": marker["library_id"],
            "layout_version": marker["layout_version"],
            "workflow_type": config.workflow_type,
            "next_benefit_number": marker.get("next_benefit_number"),
            "next_pool_number": marker.get("next_pool_number"),
            "config_updated": overlay_layout_upgraded,
            "missing_directories": missing,
        }

    def add_benefit(self, config_id: str, request: AddBenefitRequest) -> dict[str, Any]:
        with self._lock, self.config_store.lock:
            config = self.config_store.load(config_id)
            if config.workflow_type != "taobao_flash":
                raise LibraryError("通用视频库不支持新增利益点，请新增视频库")
            root = Path(config.source_root).expanduser().resolve()
            marker = self._load_marker(root)
            recent = marker.setdefault("recent_requests", {})
            if not isinstance(recent, dict):
                recent = {}
                marker["recent_requests"] = recent
            previous = recent.get(request.client_request_id)
            if isinstance(previous, dict) and previous.get("category") in config.sources:
                return self._benefit_result(
                    config,
                    root,
                    marker,
                    str(previous["category"]),
                    idempotent=True,
                )

            if self.config_store.content_hash(config_id) != request.current_config_hash:
                raise LibraryConflictError("配置已被修改，请刷新后重试")
            benefits = [key for key in config.sources if is_benefit_category(key)]
            if len(benefits) >= 20:
                raise LibraryError("利益点段最多 20 个")

            number = self._next_benefit_number(root, config, marker)
            category = f"benefit_{number}"
            relative = Path("切片素材/利益点") / str(number)
            target = root / relative
            benefits_root = root / "切片素材/利益点"
            if not benefits_root.is_dir():
                raise LibraryError("利益点根目录缺失，请先修复视频库")
            if not _within(root, benefits_root) or not _within(root, target):
                raise LibraryError("利益点目录越过视频库边界")
            if target.is_symlink():
                raise LibraryError("利益点目标不能是符号链接")

            created = False
            if target.exists():
                if not target.is_dir():
                    raise LibraryConflictError(f"利益点目标不是文件夹: {target}")
            else:
                target.mkdir()
                created = True

            updated = config.model_copy(deep=True)
            updated.sources[category] = SourceGroupConfig(
                mode=SourceMode.REQUIRED,
                directory=str(relative),
                extensions=[".mp4"],
                default_weight=1,
                items=[],
            )
            ending_index = updated.timeline.index("ending")
            updated.timeline.insert(ending_index, category)
            try:
                self.config_store.save_config(config_id, updated)
            except Exception:
                if created:
                    try:
                        target.rmdir()
                    except OSError:
                        pass
                raise

            marker["next_benefit_number"] = number + 1
            recent[request.client_request_id] = {
                "category": category,
                "created_at": _now(),
            }
            marker["recent_requests"] = dict(list(recent.items())[-100:])
            warnings: list[str] = []
            try:
                _write_json_atomic(root / MARKER_PATH, marker)
            except OSError:
                warnings.append("利益点已创建，但库计数器写入失败；下次将从现有目录恢复")
            result = self._benefit_result(
                updated, root, marker, category, idempotent=False
            )
            result["directory_preexisted"] = not created
            result["warnings"] = warnings
            return result

    def add_pool(self, config_id: str, request: AddPoolRequest) -> dict[str, Any]:
        with self._lock, self.config_store.lock:
            if not request.label.strip():
                raise LibraryError("视频库名称不能为空")
            config, root, marker = self._generic_library(config_id)
            recent = marker.setdefault("recent_requests", {})
            if not isinstance(recent, dict):
                recent = {}
                marker["recent_requests"] = recent
            previous = recent.get(request.client_request_id)
            if isinstance(previous, dict) and previous.get("pool_id") in config.sources:
                return self._pool_result(
                    config,
                    root,
                    str(previous["pool_id"]),
                    idempotent=True,
                )
            self._check_hash(config_id, request.current_config_hash)
            if len(config.sources) >= MAX_GENERIC_POOLS:
                raise LibraryError(f"通用视频库最多 {MAX_GENERIC_POOLS} 个素材库")

            number = self._next_pool_number(root, config, marker)
            pool_id = f"pool_{number}"
            relative = Path("视频库") / pool_id
            target = root / relative
            pools_root = root / "视频库"
            if not pools_root.is_dir():
                raise LibraryError("视频库根目录缺失，请先修复项目库")
            if not _within(root, target) or target.is_symlink():
                raise LibraryError("视频库目录无效或越过项目库边界")
            created = False
            if target.exists():
                if not target.is_dir():
                    raise LibraryConflictError(f"视频库目标不是文件夹: {target}")
            else:
                target.mkdir()
                created = True

            updated = config.model_copy(deep=True)
            updated.sources[pool_id] = SourceGroupConfig(
                label=request.label.strip(),
                description=request.description.strip(),
                mode=request.mode,
                directory=str(relative),
                extensions=[".mp4", ".mov", ".mkv"],
                default_weight=request.default_weight,
                items=[],
            )
            updated.timeline.append(pool_id)
            try:
                self.config_store.save_config(config_id, updated)
            except Exception:
                if created:
                    try:
                        target.rmdir()
                    except OSError:
                        pass
                raise

            marker["next_pool_number"] = number + 1
            recent[request.client_request_id] = {
                "pool_id": pool_id,
                "created_at": _now(),
            }
            marker["recent_requests"] = dict(list(recent.items())[-100:])
            warnings: list[str] = []
            try:
                _write_json_atomic(root / MARKER_PATH, marker)
            except OSError:
                warnings.append("视频库已创建，但编号计数器写入失败；下次将自动恢复")
            result = self._pool_result(updated, root, pool_id, idempotent=False)
            result["directory_preexisted"] = not created
            result["warnings"] = warnings
            return result

    def update_pool(
        self, config_id: str, pool_id: str, request: UpdatePoolRequest
    ) -> dict[str, Any]:
        with self._lock, self.config_store.lock:
            config, root, _marker = self._generic_library(config_id)
            self._check_hash(config_id, request.current_config_hash)
            if pool_id not in config.sources or not is_pool_category(pool_id):
                raise LibraryError(f"视频库不存在: {pool_id}")
            updated = config.model_copy(deep=True)
            group = updated.sources[pool_id]
            if request.label is not None:
                if not request.label.strip():
                    raise LibraryError("视频库名称不能为空")
                group.label = request.label.strip()
            if request.description is not None:
                group.description = request.description.strip()
            if request.mode is not None:
                group.mode = request.mode
            if request.default_weight is not None:
                group.default_weight = request.default_weight
            self.config_store.save_config(config_id, updated)
            return self._pool_result(updated, root, pool_id, idempotent=False)

    def delete_pool(
        self, config_id: str, pool_id: str, request: DeletePoolRequest
    ) -> dict[str, Any]:
        with self._lock, self.config_store.lock:
            config, root, _marker = self._generic_library(config_id)
            self._check_hash(config_id, request.current_config_hash)
            if pool_id not in config.sources or not is_pool_category(pool_id):
                raise LibraryError(f"视频库不存在: {pool_id}")
            retained_directory = resolve_directory(
                config, config.sources[pool_id].directory
            ).resolve()
            if not _within(root, retained_directory):
                raise LibraryError("视频库目录越过项目库边界")
            updated = config.model_copy(deep=True)
            del updated.sources[pool_id]
            updated.timeline = [item for item in updated.timeline if item != pool_id]
            naming_categories = updated.output.naming.source_metadata.categories
            if pool_id in naming_categories:
                naming_categories.remove(pool_id)
                if not naming_categories:
                    naming_categories.append("pool_*")
            self.config_store.save_config(config_id, updated)
            return {
                "ok": True,
                "pool_id": pool_id,
                "retained_directory": str(retained_directory),
                "config": updated.model_dump(mode="json"),
                "content_hash": self.config_store.content_hash(config_id),
            }

    def reorder_timeline(
        self, config_id: str, request: ReorderTimelineRequest
    ) -> dict[str, Any]:
        with self._lock, self.config_store.lock:
            config, _root, _marker = self._generic_library(config_id)
            self._check_hash(config_id, request.current_config_hash)
            if len(request.timeline) != len(set(request.timeline)):
                raise LibraryError("拼接顺序中不能包含重复视频库")
            if set(request.timeline) != set(config.sources):
                raise LibraryError("拼接顺序必须且只能包含当前全部视频库")
            updated = config.model_copy(deep=True)
            updated.timeline = list(request.timeline)
            self.config_store.save_config(config_id, updated)
            return {
                "ok": True,
                "timeline": updated.timeline,
                "config": updated.model_dump(mode="json"),
                "content_hash": self.config_store.content_hash(config_id),
            }

    def replace_overlay_image(
        self, config_id: str, request: ReplaceOverlayImageRequest
    ) -> dict[str, Any]:
        with self._lock, self.config_store.lock:
            config = self.config_store.load(config_id)
            self._check_hash(config_id, request.current_config_hash)
            root = Path(config.source_root).expanduser().resolve()
            marker = self._load_marker(root)
            if config.workflow_type == "generic":
                self._ensure_generic_overlay_layout(root, marker)
            paths = marker.get("paths")
            relative_value = paths.get("overlays") if isinstance(paths, dict) else None
            if not isinstance(relative_value, str) or not relative_value.strip():
                raise LibraryError("项目库没有风险提示语图片目录")

            filename = request.filename.strip()
            if Path(filename).name != filename or INVALID_FOLDER_CHARACTERS.search(filename):
                raise LibraryError("图片文件名包含非法字符")
            extension = Path(filename).suffix.lower()
            if extension not in OVERLAY_IMAGE_EXTENSIONS:
                raise LibraryError("风险提示语图片仅支持 PNG、JPG、WEBP 或 BMP")
            if len(request.data_base64) > MAX_OVERLAY_IMAGE_BYTES * 2:
                raise LibraryError("风险提示语图片不能超过 20 MB")
            try:
                content = base64.b64decode(request.data_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise LibraryError("风险提示语图片数据无效") from exc
            if not content or len(content) > MAX_OVERLAY_IMAGE_BYTES:
                raise LibraryError("风险提示语图片不能为空且不能超过 20 MB")

            relative = Path(relative_value)
            overlay_directory = root / relative
            if (
                relative.is_absolute()
                or not _within(root, overlay_directory)
                or overlay_directory.is_symlink()
            ):
                raise LibraryError("风险提示语图片目录无效或越过项目库边界")
            overlay_directory.mkdir(parents=True, exist_ok=True)
            target = overlay_directory / filename
            if target.is_symlink() or not _within(root, target):
                raise LibraryError("风险提示语图片目标无效")

            temporary = overlay_directory / f".{uuid.uuid4().hex}.upload{extension}"
            temporary.write_bytes(content)
            try:
                from .scanner import probe_media

                probe_media(temporary)
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                raise LibraryError(f"无法读取风险提示语图片: {exc}") from exc

            existing = [
                path
                for path in overlay_directory.iterdir()
                if path.is_file()
                and not path.name.startswith(".")
                and path.suffix.lower() in OVERLAY_IMAGE_EXTENSIONS
            ]
            backup_paths: list[str] = []
            if existing:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                backup_directory = root / ".smartstitch" / "overlay-backups" / stamp
                backup_directory.mkdir(parents=True, exist_ok=False)
                for previous in existing:
                    destination = backup_directory / previous.name
                    previous.replace(destination)
                    backup_paths.append(str(destination))
            temporary.replace(target)

            updated = config.model_copy(deep=True)
            updated.benefit_overlays.mode = SourceMode.REQUIRED
            updated.benefit_overlays.file = ""
            if updated.workflow_type == "generic":
                updated.benefit_overlays.timing.scope = "full"
            self.config_store.save_config(config_id, updated)
            return {
                "ok": True,
                "filename": filename,
                "directory": str(overlay_directory),
                "path": str(target),
                "backups": backup_paths,
                "config": updated.model_dump(mode="json"),
                "content_hash": self.config_store.content_hash(config_id),
            }

    def slice_targets(self, config_id: str) -> list[dict[str, str]]:
        config = self.config_store.load(config_id)
        root = Path(config.source_root).expanduser().resolve()
        self._load_marker(root)
        unclassified = (
            root / "未归类"
            if config.workflow_type == "generic"
            else root / "切片素材/未归类"
        )
        targets = [
            {
                "category": "unclassified",
                "label": "未归类",
                "directory": str(unclassified),
            }
        ]
        labels = {"pre_roll": "前贴", "hook": "引子", "ending": "结尾", "end_card": "尾帧"}
        for category in config.timeline:
            if category not in config.sources:
                continue
            target = self.resolve_slice_target(config_id, category)
            number = category.split("_", 1)[1] if is_benefit_category(category) else ""
            group_label = config.sources[category].label.strip()
            targets.append(
                {
                    "category": category,
                    "label": group_label
                    or (f"利益点 {number}" if number else labels.get(category, category)),
                    "directory": str(target),
                }
            )
        return targets

    def open_source_directory(self, config_id: str, category: str) -> dict[str, Any]:
        config = self.config_store.load(config_id)
        group = config.sources.get(category)
        if group is None:
            raise LibraryError(f"配置中不存在视频库: {category}")
        directory = resolve_directory(config, group.directory)
        result = open_directory_in_file_manager(directory)
        return {
            **result,
            "category": category,
            "folder_name": directory.name,
        }

    def resolve_slice_target(self, config_id: str, category: str) -> Path:
        config = self.config_store.load(config_id)
        root = Path(config.source_root).expanduser().resolve()
        self._load_marker(root)
        if category == "unclassified":
            unresolved_target = (
                root / "未归类"
                if config.workflow_type == "generic"
                else root / "切片素材/未归类"
            )
        elif category in config.sources and (
            (config.workflow_type == "generic" and is_pool_category(category))
            or category in {"pre_roll", "hook", "ending", "end_card"}
            or is_benefit_category(category)
        ):
            unresolved_target = resolve_directory(
                config, config.sources[category].directory
            )
        else:
            raise LibraryError(f"不支持的切片入库类别: {category}")
        if unresolved_target.is_symlink():
            raise LibraryError("切片目标目录不能是符号链接")
        target = unresolved_target.resolve()
        if not _within(root, target):
            raise LibraryError("切片目标目录越过视频库边界")
        if not target.is_dir():
            raise LibraryError(f"切片目标目录不存在: {target}")
        return target

    def _load_marker(self, root: Path) -> dict[str, Any]:
        marker_path = root / MARKER_PATH
        if not marker_path.is_file():
            raise LibraryError("当前配置不是 SmartStitch 受管视频库")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict) or marker.get("layout_version") not in {
            LAYOUT_VERSION,
            GENERIC_LAYOUT_VERSION,
        }:
            raise LibraryError("视频库标记版本不受支持")
        if not isinstance(marker.get("library_id"), str):
            raise LibraryError("视频库标记缺少 library_id")
        if marker["layout_version"] == LAYOUT_VERSION:
            if not isinstance(marker.get("next_benefit_number"), int):
                raise LibraryError("视频库标记缺少利益点编号")
        elif not isinstance(marker.get("next_pool_number"), int):
            raise LibraryError("视频库标记缺少视频库编号")
        paths = marker.get("paths")
        if not isinstance(paths, dict):
            raise LibraryError("视频库标记缺少路径定义")
        for value in paths.values():
            relative = Path(str(value))
            if relative.is_absolute() or not _within(root, root / relative):
                raise LibraryError("视频库标记包含越界路径")
        return marker

    def _generic_library(
        self, config_id: str
    ) -> tuple[AppConfig, Path, dict[str, Any]]:
        config = self.config_store.load(config_id)
        if config.workflow_type != "generic":
            raise LibraryError("仅通用项目支持自定义视频库")
        root = Path(config.source_root).expanduser().resolve()
        marker = self._load_marker(root)
        if marker.get("layout_version") != GENERIC_LAYOUT_VERSION:
            raise LibraryError("通用项目标记版本不正确")
        self._ensure_generic_overlay_layout(root, marker)
        return config, root, marker

    def _ensure_generic_overlay_layout(
        self, root: Path, marker: dict[str, Any]
    ) -> bool:
        """Add the overlay directory to generic libraries created before this feature."""
        paths = marker.get("paths")
        if not isinstance(paths, dict):
            raise LibraryError("视频库标记缺少路径定义")
        relative = Path(str(paths.get("overlays") or "风险提示语图片"))
        target = root / relative
        if relative.is_absolute() or not _within(root, target) or target.is_symlink():
            raise LibraryError("风险提示语图片目录无效或越过项目库边界")
        if target.exists() and not target.is_dir():
            raise LibraryError(f"风险提示语图片目标不是文件夹: {target}")
        target.mkdir(parents=True, exist_ok=True)
        upgraded = paths.get("overlays") != str(relative)
        if upgraded:
            paths["overlays"] = str(relative)
            _write_json_atomic(root / MARKER_PATH, marker)
        return upgraded

    def _check_hash(self, config_id: str, expected: str) -> None:
        if self.config_store.content_hash(config_id) != expected:
            raise LibraryConflictError("配置已被修改，请刷新后重试")

    def _next_pool_number(
        self, root: Path, config: AppConfig, marker: dict[str, Any]
    ) -> int:
        configured = [
            int(category.split("_", 1)[1])
            for category in config.sources
            if is_pool_category(category)
        ]
        directory_numbers: list[int] = []
        pools_root = root / "视频库"
        if pools_root.is_dir():
            directory_numbers = [
                int(path.name.split("_", 1)[1])
                for path in pools_root.iterdir()
                if path.is_dir() and is_pool_category(path.name)
            ]
        return max(
            int(marker.get("next_pool_number", 1)),
            max(configured, default=0) + 1,
            max(directory_numbers, default=0) + 1,
        )

    def _next_benefit_number(
        self, root: Path, config: AppConfig, marker: dict[str, Any]
    ) -> int:
        configured = [
            int(category.split("_", 1)[1])
            for category in config.sources
            if is_benefit_category(category)
        ]
        directory_numbers: list[int] = []
        benefits_root = root / "切片素材/利益点"
        if benefits_root.is_dir():
            directory_numbers = [
                int(path.name)
                for path in benefits_root.iterdir()
                if path.is_dir() and path.name.isdigit() and int(path.name) > 0
            ]
        return max(
            int(marker.get("next_benefit_number", 1)),
            max(configured, default=0) + 1,
            max(directory_numbers, default=0) + 1,
        )

    def _idempotent_create_result(
        self, root: Path, request: CreateLibraryRequest
    ) -> dict[str, Any] | None:
        marker_path = root / MARKER_PATH
        if not marker_path.is_file():
            return None
        try:
            marker = self._load_marker(root)
        except (OSError, json.JSONDecodeError, LibraryError):
            return None
        if marker.get("create_request_id") != request.client_request_id:
            return None
        try:
            config = self.config_store.load(request.new_id)
        except (ConfigError, FileNotFoundError):
            return None
        return self._create_result(root, config, marker, idempotent=True)

    def _create_result(
        self,
        root: Path,
        config: AppConfig,
        marker: dict[str, Any],
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "idempotent": idempotent,
            "root_path": str(root),
            "library_id": marker["library_id"],
            "config": config.model_dump(mode="json"),
            "content_hash": self.config_store.content_hash(config.id),
        }

    def _benefit_result(
        self,
        config: AppConfig,
        root: Path,
        marker: dict[str, Any],
        category: str,
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        number = int(category.split("_", 1)[1])
        target = root / "切片素材/利益点" / str(number)
        return {
            "ok": True,
            "idempotent": idempotent,
            "category": category,
            "directory": str(target),
            "library_id": marker["library_id"],
            "config": config.model_dump(mode="json"),
            "content_hash": self.config_store.content_hash(config.id),
        }

    def _pool_result(
        self,
        config: AppConfig,
        root: Path,
        pool_id: str,
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        group = config.sources[pool_id]
        return {
            "ok": True,
            "idempotent": idempotent,
            "pool_id": pool_id,
            "directory": str(resolve_directory(config, group.directory).resolve()),
            "library_id": self._load_marker(root)["library_id"],
            "config": config.model_dump(mode="json"),
            "content_hash": self.config_store.content_hash(config.id),
        }

    def _cleanup_owned_tree(self, root: Path, request_id: str) -> None:
        marker_path = root / MARKER_PATH
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if root.name.startswith(".smartstitch-create-"):
                self._remove_empty_tree(root)
            return
        if marker.get("create_request_id") != request_id:
            return
        unexpected = [
            path
            for path in root.rglob("*")
            if (
                (path.is_file() or path.is_symlink())
                and path != marker_path
                and not (
                    path.parent == root
                    and path.name in IGNORABLE_SELECTED_ROOT_NAMES
                )
            )
        ]
        if unexpected:
            return
        marker_path.unlink(missing_ok=True)
        self._remove_empty_tree(root)

    @staticmethod
    def _remove_empty_tree(root: Path) -> None:
        if not root.exists() or not root.is_dir():
            return
        directories = [path for path in root.rglob("*") if path.is_dir()]
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                return
        try:
            root.rmdir()
        except OSError:
            pass


def pick_directory() -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "选择文件夹")',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if result.returncode != 0:
            if "-128" in result.stderr or "User canceled" in result.stderr:
                return {"cancelled": True, "path": None}
            raise LibraryError(result.stderr.strip() or "无法打开目录选择器")
        return {"cancelled": False, "path": result.stdout.strip().rstrip("/")}
    raise LibraryError("当前系统暂不支持图形目录选择器，请手动输入绝对路径")
