from __future__ import annotations

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
    AppConfig,
    CreateLibraryRequest,
    LibraryPreflightRequest,
    SourceGroupConfig,
    SourceMode,
    is_benefit_category,
    resolve_directory,
)


LAYOUT_VERSION = 1
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
INVALID_FOLDER_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00]')


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
        root = parent / name
        if root.parent != parent:
            raise LibraryError("视频库必须是保存位置的直接子目录")
        if root.exists() or root.is_symlink():
            raise LibraryConflictError(f"同名视频库已存在: {root}")
        return {
            "ok": True,
            "parent_directory": str(parent),
            "folder_name": name,
            "root_path": str(root),
            "directories": [str(path) for path in STANDARD_DIRECTORIES],
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
            root = parent / name

            existing = self._idempotent_create_result(root, request)
            if existing is not None:
                return existing

            self.preflight(
                LibraryPreflightRequest(
                    parent_directory=str(parent), folder_name=name
                )
            )
            if self.config_store.path_for(request.new_id).exists():
                raise LibraryConflictError(f"配置已存在: {request.new_id}")

            temporary = Path(tempfile.mkdtemp(prefix=".smartstitch-create-", dir=parent))
            committed = False
            try:
                for relative in STANDARD_DIRECTORIES:
                    (temporary / relative).mkdir(parents=True, exist_ok=True)
                marker = _marker_payload(request)
                _write_json_atomic(temporary / MARKER_PATH, marker)
                config = _standard_config(request, root)
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
        except (OSError, json.JSONDecodeError, LibraryError) as exc:
            return {
                "managed": True,
                "health": "invalid_marker",
                "root_path": str(root),
                "error": str(exc),
                "missing_directories": [],
            }
        required = [root / relative for relative in STANDARD_DIRECTORIES]
        missing = [str(path) for path in required if not path.is_dir()]
        return {
            "managed": True,
            "health": "healthy" if not missing else "needs_repair",
            "root_path": str(root.resolve()),
            "library_id": marker["library_id"],
            "layout_version": marker["layout_version"],
            "next_benefit_number": marker["next_benefit_number"],
            "missing_directories": missing,
        }

    def add_benefit(self, config_id: str, request: AddBenefitRequest) -> dict[str, Any]:
        with self._lock, self.config_store.lock:
            config = self.config_store.load(config_id)
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

    def slice_targets(self, config_id: str) -> list[dict[str, str]]:
        config = self.config_store.load(config_id)
        root = Path(config.source_root).expanduser().resolve()
        self._load_marker(root)
        targets = [
            {
                "category": "unclassified",
                "label": "未归类",
                "directory": str(root / "切片素材/未归类"),
            }
        ]
        labels = {"pre_roll": "前贴", "hook": "引子", "ending": "结尾", "end_card": "尾帧"}
        for category in config.timeline:
            if category not in config.sources:
                continue
            target = self.resolve_slice_target(config_id, category)
            number = category.split("_", 1)[1] if is_benefit_category(category) else ""
            targets.append(
                {
                    "category": category,
                    "label": f"利益点 {number}" if number else labels.get(category, category),
                    "directory": str(target),
                }
            )
        return targets

    def resolve_slice_target(self, config_id: str, category: str) -> Path:
        config = self.config_store.load(config_id)
        root = Path(config.source_root).expanduser().resolve()
        self._load_marker(root)
        if category == "unclassified":
            unresolved_target = root / "切片素材/未归类"
        elif category in config.sources and (
            category in {"pre_roll", "hook", "ending", "end_card"}
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
        if not isinstance(marker, dict) or marker.get("layout_version") != LAYOUT_VERSION:
            raise LibraryError("视频库标记版本不受支持")
        if not isinstance(marker.get("library_id"), str):
            raise LibraryError("视频库标记缺少 library_id")
        if not isinstance(marker.get("next_benefit_number"), int):
            raise LibraryError("视频库标记缺少利益点编号")
        paths = marker.get("paths")
        if not isinstance(paths, dict):
            raise LibraryError("视频库标记缺少路径定义")
        for value in paths.values():
            relative = Path(str(value))
            if relative.is_absolute() or not _within(root, root / relative):
                raise LibraryError("视频库标记包含越界路径")
        return marker

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
            if (path.is_file() or path.is_symlink()) and path != marker_path
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
