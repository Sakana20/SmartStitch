"""Deterministic A-then-B pairing for the toolbox folder concatenation task."""

from __future__ import annotations

from pathlib import Path

from .models import AppConfig, Asset, FolderConcatRequest
from .naming import append_duplicate_suffix, derive_plan_naming, render_plan_filename

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


def _videos(directory: Path) -> list[Path]:
    return sorted(
        (
            path for path in directory.iterdir()
            if path.is_file() and not path.name.startswith((".", "._", ".~"))
            and path.suffix.lower() in VIDEO_SUFFIXES
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )


def inspect_folders(request: FolderConcatRequest) -> dict:
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
        "unpaired_a": len(videos_a) - pair_count,
        "unpaired_b": len(videos_b) - pair_count,
        "pairs": [
            {"a": str(a), "b": str(b), "a_name": a.name, "b_name": b.name}
            for a, b in zip(videos_a, videos_b)
        ],
    }

    result["naming"] = request.naming.model_dump(mode="json") if request.naming else None
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
    return result


def folder_concat_config(preview: dict, output_root: Path) -> AppConfig:
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
        "benefit_overlays": {"mode": "disabled", "file": ""},
        "output": {"directory": str(output_root), "video_codec": "h264_videotoolbox",
                   "naming": preview.get("naming") or {}},
        "batch": {"minimum_free_space_gb": 0.5, "retry_count": 0},
    })
