"""Create an independent generic library from a managed Taobao Flash library.

Run with --dry-run first. The source library is only read, and existing target
directories or configuration IDs are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path

from smartstitch.config import ConfigStore
from smartstitch.library import LibraryService
from smartstitch.models import AppConfig, CreateLibraryRequest, SourceMode


LABELS = {
    "pre_roll": "前贴",
    "hook": "引子",
    "ending": "结尾",
    "end_card": "尾帧",
}


def prepare(source: AppConfig, target_id: str, target_name: str, target_root: Path) -> tuple[AppConfig, list[tuple[Path, Path]]]:
    if source.workflow_type != "taobao_flash":
        raise ValueError("源项目必须是淘宝闪购")
    if source.sources.get("end_card") and source.sources["end_card"].mode != SourceMode.DISABLED:
        raise ValueError("启用的尾帧暂不能迁移：通用库不支持图片视频库")
    source_root = Path(source.source_root).resolve()
    result = source.model_copy(deep=True)
    result.id = target_id
    result.name = target_name
    result.schema_version = 3
    result.workflow_type = "generic"
    result.source_root = str(target_root)
    result.output.directory = str(target_root / "成片输出")
    result.output.feishu_base_sync.field_schema = "taobao_flash"
    result.timeline = []
    result.sources = {}
    copies: list[tuple[Path, Path]] = []
    for number, old_id in enumerate(source.timeline, start=1):
        old_group = source.sources[old_id]
        if old_id == "end_card":
            continue
        pool_id = f"pool_{number}"
        group = old_group.model_copy(deep=True)
        group.label = old_group.label or (
            f"利益点 {old_id.split('_', 1)[1]}"
            if old_id.startswith("benefit_")
            else LABELS.get(old_id, old_id.replace("_", " "))
        )
        group.directory = f"视频库/{pool_id}"
        old_dir = (source_root / old_group.directory).resolve()
        new_dir = target_root / group.directory
        if not old_dir.is_dir() or not old_dir.is_relative_to(source_root):
            raise ValueError(f"源素材目录无效: {old_dir}")
        for item in group.items:
            old_path = Path(item.path).resolve()
            if not old_path.is_relative_to(old_dir):
                raise ValueError(f"素材条目越过源目录: {old_path}")
            item.path = str(new_dir / old_path.relative_to(old_dir))
        result.timeline.append(pool_id)
        result.sources[pool_id] = group
        copies.append((old_dir, new_dir))
    for directory in ("原始视频", "风险提示语图片"):
        old_dir = source_root / directory
        if old_dir.is_dir():
            copies.append((old_dir, target_root / directory))
    if result.benefit_overlays.file:
        overlay_file = Path(result.benefit_overlays.file)
        if overlay_file.is_absolute() and overlay_file.is_relative_to(source_root):
            result.benefit_overlays.file = str(target_root / overlay_file.relative_to(source_root))
    result = AppConfig.model_validate(result.model_dump(mode="json"))
    return result, copies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-directory", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--target-name", required=True)
    parser.add_argument("--target-folder", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    store = ConfigStore(args.config_directory)
    source = store.load(args.source_id)
    target_root = args.config_directory / args.target_folder
    if target_root.exists() or store.path_for(args.target_id).exists():
        raise ValueError("目标目录或配置 ID 已存在，停止以避免覆盖")
    target, copies = prepare(source, args.target_id, args.target_name, target_root)
    summary = {
        "target_id": target.id,
        "target_root": str(target_root),
        "pools": [{"id": key, "label": target.sources[key].label} for key in target.timeline],
        "copy_directories": [str(src) for src, _ in copies],
        "feishu_field_schema": target.output.feishu_base_sync.field_schema,
    }
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    service = LibraryService(store)
    service.create(CreateLibraryRequest(
        parent_directory=str(args.config_directory),
        folder_name=args.target_folder,
        workflow_type="generic",
        new_id=args.target_id,
        new_name=args.target_name,
        client_request_id=f"clone-{args.target_id}-{uuid.uuid4().hex}",
    ))
    for old_dir, new_dir in copies:
        shutil.copytree(old_dir, new_dir, dirs_exist_ok=True, copy_function=shutil.copy2)
    marker_path = target_root / ".smartstitch/library.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["next_pool_number"] = len(target.timeline) + 1
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    store.save_config(args.target_id, target)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
