"""Deterministic A-then-B pairing for the toolbox folder concatenation task."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PIL import Image

from .config import ConfigStore
from .models import AppConfig, Asset, FolderConcatRequest, SourceMode
from .naming import append_duplicate_suffix, derive_plan_naming, filename_tokens, render_plan_filename
from .probe_cache import MediaProbeCache
from .scanner import scan_config, scan_fixed_overlay

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".bmp": "BMP"}
MAX_OVERLAY_BYTES = 20 * 1024 * 1024


def _videos(directory: Path) -> list[Path]:
    return sorted(
        (
            path for path in directory.iterdir()
            if path.is_file() and not path.name.startswith((".", "._", ".~"))
            and path.suffix.lower() in VIDEO_SUFFIXES
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )


def inspect_folders(
    request: FolderConcatRequest, *, validate_overlay: bool = True,
    probe_cache: MediaProbeCache | None = None,
    config_store: ConfigStore | None = None,
) -> dict:
    directory_a = Path(request.directory_a).expanduser().resolve()
    directory_b = Path(request.directory_b).expanduser().resolve()
    if not directory_a.is_dir():
        raise ValueError("A 文件夹不存在或不是文件夹")
    if not directory_b.is_dir():
        raise ValueError("B 文件夹不存在或不是文件夹")
    if directory_a == directory_b:
        raise ValueError("A 和 B 需要选择不同的文件夹")
    videos_a = _videos(directory_a)
    videos_b = _videos(directory_b)
    if not videos_a:
        raise ValueError("A 文件夹内没有支持的视频")
    if not videos_b:
        raise ValueError("B 文件夹内没有支持的视频")
    pair_count = min(len(videos_a), len(videos_b))
    result = {
        "directory_a": str(directory_a),
        "directory_b": str(directory_b),
        "count_a": len(videos_a),
        "count_b": len(videos_b),
        "pair_count": pair_count,
        "files_a": [path.name for path in videos_a],
        "files_b": [path.name for path in videos_b],
        "unpaired_a": len(videos_a) - pair_count,
        "unpaired_b": len(videos_b) - pair_count,
        "pairs": [
            {"a": str(a), "b": str(b), "a_name": a.name, "b_name": b.name}
            for a, b in zip(videos_a, videos_b)
        ],
    }
    result["naming"] = request.naming.model_dump(mode="json") if request.naming else None
    complete_folder_naming(result)
    result["benefit_overlays"] = (
        {**request.benefit_overlays.model_dump(mode="json"), "enabled": True}
        if request.benefit_overlays else {"enabled": False}
    )
    result["warnings"] = []
    if request.benefit_overlays and request.benefit_overlays.config_id:
        if config_store is None:
            raise ValueError("无法读取所选配置的风险提示语图片")
        source_config, asset = config_overlay_image(config_store, request.benefit_overlays.config_id)
        result["benefit_overlays"].update({
            "file": asset.path, "source_config_id": source_config.id,
            "source_config_name": source_config.name,
        })
    config = folder_concat_config(result, directory_a.parent)
    used_names: set[str] = set()
    for index, pair in enumerate(result["pairs"], start=1):
        metadata = None
        if config.output.naming.enabled:
            selections = {
                category: Asset(id=pair[key], category=category, path=pair[key],
                                name=pair[f"{key}_name"], media_type="video")
                for category, key in (("pool_1", "a"), ("pool_2", "b"))
            }
            metadata = derive_plan_naming(config, selections, config.output.naming.sequence_start + index - 1)
            base_name = render_plan_filename(config, metadata)
        else:
            base_name = f"{Path(pair['a_name']).stem}_拼接.mp4"
        output_name = base_name
        serial = 1
        while output_name.casefold() in used_names:
            serial += 1
            if config.output.naming.enabled:
                candidate = append_duplicate_suffix(config, base_name, serial)
                if candidate.casefold() == output_name.casefold():
                    raise ValueError("重名后缀必须包含递增序号")
                output_name = candidate
            else:
                output_name = f"{Path(base_name).stem}_{serial}.mp4"
        used_names.add(output_name.casefold())
        pair["output_name"] = output_name
        pair["naming"] = metadata.model_dump(mode="json") if metadata else None
    if validate_overlay and request.benefit_overlays:
        prepare_folder_concat(result, directory_a.parent, probe_cache=probe_cache)
    return result


def complete_folder_naming(preview: dict) -> None:
    """Freeze same-position mappings for mixed field counts in this folder batch."""
    naming = preview.get("naming")
    if not naming or not naming.get("enabled") or not naming.get("builder", {}).get("enabled"):
        return
    for block in naming["builder"]["blocks"]:
        if block["type"] != "source" or block["category"] not in {"pool_1", "pool_2"}:
            continue
        key = "a" if block["category"] == "pool_1" else "b"
        variants = block["variants"]
        positions = {variant["field_index"] for variant in variants}
        for index, pair in enumerate(preview["pairs"], start=1):
            name = pair[f"{key}_name"]
            try:
                fields = filename_tokens(name)
            except ValueError as exc:
                raise ValueError(f"第 {index} 组 {key.upper()} 文件夹命名失败：{name}：{exc}") from exc
            if any(variant["field_count"] == len(fields) for variant in variants):
                continue
            if len(positions) != 1:
                raise ValueError(
                    f"第 {index} 组 {key.upper()} 文件夹命名失败：{name} 有 {len(fields)} 个字段，"
                    "该命名块的不同格式使用了不同字段位置，请为此格式明确选择片段"
                )
            position = next(iter(positions))
            if position >= len(fields):
                raise ValueError(
                    f"第 {index} 组 {key.upper()} 文件夹命名失败：{name} 只有 {len(fields)} 个字段，"
                    f"无法读取所选第 {position + 1} 个字段（分隔符 |!|）"
                )
            variants.append({"sample_name": name, "field_count": len(fields), "field_index": position})


def folder_concat_config(preview: dict, output_root: Path) -> AppConfig:
    overlay = preview.get("benefit_overlays") or {}
    settings = (
        {key: overlay[key] for key in ("mode", "file", "placement", "timing")}
        if overlay.get("enabled") else {"mode": "disabled", "file": ""}
    )
    if overlay.get("enabled") and settings["timing"]["scope"] in {"a", "b"}:
        # A/B ranges belong to each pair and are passed to the renderer as PlanItem.overlay_scope.
        settings["timing"] = {"scope": "full", "start_seconds": 0, "end_seconds": None}
    return AppConfig.model_validate({
        "schema_version": 3, "workflow_type": "generic",
        "id": "folder-concat", "name": "文件夹拼接",
        "source_root": preview["directory_a"],
        "timeline": ["pool_1", "pool_2"],
        "sources": {
            "pool_1": {"label": "A 文件夹", "mode": "required",
                       "directory": preview["directory_a"], "extensions": sorted(VIDEO_SUFFIXES)},
            "pool_2": {"label": "B 文件夹", "mode": "required",
                       "directory": preview["directory_b"], "extensions": sorted(VIDEO_SUFFIXES)},
        },
        "benefit_overlays": settings,
        "output": {"directory": str(output_root), "video_codec": "h264_videotoolbox",
                   "naming": preview.get("naming") or {}},
        "batch": {"minimum_free_space_gb": 0.5, "retry_count": 0},
    })


def _file_fingerprint(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def overlay_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_OVERLAY_BYTES:
                raise ValueError("风险提示语图片不能超过 20 MB")
            digest.update(chunk)
    return digest.hexdigest()


def validate_overlay_image(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"风险提示语图片不存在或不是文件: {path}")
    expected_format = IMAGE_FORMATS.get(path.suffix.lower())
    if expected_format is None:
        raise ValueError("风险提示语图片仅支持静态 PNG、JPG、WEBP 或 BMP")
    before = _file_fingerprint(path)
    if not 0 < before[2] <= MAX_OVERLAY_BYTES:
        raise ValueError("风险提示语图片不能为空且不能超过 20 MB")
    try:
        with Image.open(path) as image:
            if image.format != expected_format:
                raise ValueError("风险提示语图片实际格式与扩展名不一致")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("风险提示语图片仅支持静态单帧图片")
            image.verify()
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            if width <= 0 or height <= 0:
                raise ValueError("风险提示语图片尺寸无效")
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"风险提示语图片不可用: {exc}") from exc
    digest = overlay_sha256(path)
    if before != _file_fingerprint(path):
        raise ValueError("读取时风险提示语图片发生变化，请重新提交")
    return {"source_path": str(path), "name": path.name, "width": width, "height": height,
            "size_bytes": before[2], "format": expected_format, "sha256": digest}


def prepare_folder_concat(
    preview: dict, output_root: Path, *, probe_cache: MediaProbeCache | None = None,
) -> tuple[AppConfig, dict[str, dict[str, Asset]], Asset | None]:
    """Shared media/timing preflight for overlay previews and all submissions."""
    config = folder_concat_config(preview, output_root)
    overlay_summary = preview["benefit_overlays"]
    if config.benefit_overlays.mode != SourceMode.DISABLED:
        path = Path(config.benefit_overlays.file).expanduser().resolve()
        metadata = validate_overlay_image(path)
        config.benefit_overlays.file = str(path)
        overlay_summary.update(metadata)
        overlay_summary["file"] = str(path)
    scan = scan_config(config, probe_cache=probe_cache)
    assets_by_path = {
        category: {asset.path: asset for asset in scan.assets[category]}
        for category in ("pool_1", "pool_2")
    }
    invalid = []
    for index, pair in enumerate(preview["pairs"], start=1):
        for category, key in (("pool_1", "a"), ("pool_2", "b")):
            asset = assets_by_path[category].get(pair[key])
            if asset is None or not asset.selectable:
                invalid.append(f"第 {index} 组 {pair[f'{key}_name']}: {asset.error if asset else '文件已变化或不可用'}")
    if invalid:
        raise ValueError("文件夹中有不可用视频，请先处理：" + "；".join(invalid[:5]))
    first = assets_by_path["pool_1"][preview["pairs"][0]["a"]].probe
    if first.width and first.height:
        config.output.width = max(2, first.width // 2 * 2)
        config.output.height = max(2, first.height // 2 * 2)
    if first.fps:
        config.output.fps = first.fps
    overlay = None
    if config.benefit_overlays.mode != SourceMode.DISABLED:
        overlays = scan.assets.get("benefit_overlay", [])
        if len(overlays) != 1 or not overlays[0].selectable:
            details = "；".join(error for error in scan.errors if error.startswith("benefit_overlay:"))
            raise ValueError(details or "风险提示语图片不可用")
        overlay = overlays[0]
        overlay_summary["output"] = {"width": config.output.width, "height": config.output.height, "fps": config.output.fps}
        timing = overlay_summary["timing"]
        out_of_range, truncated = [], 0
        for index, pair in enumerate(preview["pairs"], start=1):
            duration_a = assets_by_path["pool_1"][pair["a"]].probe.duration
            duration = duration_a + assets_by_path["pool_2"][pair["b"]].probe.duration
            start = timing["start_seconds"] if timing["scope"] == "custom" else duration_a if timing["scope"] == "b" else 0
            end = timing["end_seconds"] if timing["scope"] == "custom" else duration_a if timing["scope"] == "a" else None
            if start >= duration:
                out_of_range.append(f"第 {index} 组 {pair['a_name']} → {pair['b_name']}（{duration:.3f} 秒）")
            if end is not None and end > duration:
                truncated += 1
            pair["estimated_duration"] = duration
            pair["overlay_range"] = {"start_seconds": start, "end_seconds": min(end, duration) if end is not None else duration}
        if out_of_range:
            raise ValueError("风险提示语开始时间超出成片时长：" + "；".join(out_of_range[:5]))
        if truncated:
            preview["warnings"].append(f"{truncated} 组成片的风险提示语结束时间超出片尾，已截到成片结束")
        if config.benefit_overlays.placement.opacity == 0:
            preview["warnings"].append("图片透明度为 0%，成片中不可见")
    return config, assets_by_path, overlay


def config_overlay_image(store: ConfigStore, config_id: str) -> tuple[AppConfig, Asset]:
    config = store.load(config_id).model_copy(deep=True)
    # A configured image can be reused even when that project's display switch is off.
    config.benefit_overlays.mode = SourceMode.REQUIRED
    assets, errors = scan_fixed_overlay(config)
    if errors or len(assets) != 1 or not assets[0].selectable:
        raise ValueError(f"{config.name}：" + ("；".join(errors) or "没有可用的风险提示语图片"))
    validate_overlay_image(Path(assets[0].path))
    return config, assets[0]


def list_config_overlay_images(store: ConfigStore) -> list[dict]:
    choices = []
    for info in store.list():
        item = {"config_id": info["id"], "config_name": info["name"], "available": False}
        try:
            config, asset = config_overlay_image(store, str(info["id"]))
            item.update({"available": True, "config_name": config.name, "file": asset.path,
                         "name": asset.name, "width": asset.probe.width, "height": asset.probe.height})
        except (ValueError, FileNotFoundError, OSError) as exc:
            item["error"] = str(exc)
        choices.append(item)
    return choices


def freeze_folder_overlay(config: AppConfig, summary: dict, directory: Path) -> Asset | None:
    if config.benefit_overlays.mode == SourceMode.DISABLED:
        return None
    source = Path(config.benefit_overlays.file)
    before = _file_fingerprint(source)
    assets = directory / "assets"
    assets.mkdir()
    target = assets / f"risk-overlay{source.suffix.lower()}"
    temporary = target.with_name(f".{target.name}.tmp")
    digest, size = hashlib.sha256(), 0
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_OVERLAY_BYTES:
                    raise ValueError("风险提示语图片不能超过 20 MB")
                digest.update(chunk)
                writer.write(chunk)
        if (before != _file_fingerprint(source) or size != summary["size_bytes"]
                or digest.hexdigest() != summary["sha256"]):
            raise ValueError("预检后风险提示语图片发生变化，请重新提交")
        os.replace(temporary, target)
        validate_overlay_image(target)
        config.benefit_overlays.file = str(target)
        overlays, errors = scan_fixed_overlay(config)
        if errors or len(overlays) != 1 or not overlays[0].selectable:
            raise ValueError("；".join(errors) or "风险提示语图片副本不可用")
        summary["snapshot_path"] = str(target)
        return overlays[0]
    finally:
        temporary.unlink(missing_ok=True)
