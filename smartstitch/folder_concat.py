"""Deterministic A-then-B pairing for the toolbox folder concatenation task."""

from __future__ import annotations

from pathlib import Path

from .models import FolderConcatRequest

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
    return {
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
