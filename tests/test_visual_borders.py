from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from smartstitch.models import AppConfig
from smartstitch.scanner import scan_config
from smartstitch.visual_borders import (
    GLOBAL_BORDER_DIRECTORY,
    GLOBAL_BORDER_REGISTRY,
    VisualBorderLibrary,
    VisualBorderLibraryConflict,
)


def generate_border(path: Path, color: str = "red") -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            (
                "color=c=black@0.0:s=180x320:r=2:d=0.5,format=argb,"
                f"drawbox=x=0:y=0:w=iw:h=20:color={color}@1:t=fill:replace=1"
            ),
            "-c:v", "qtrle", "-pix_fmt", "argb", str(path),
        ],
        check=True,
    )


def visual_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "global-border-test",
            "name": "全局边框测试",
            "source_root": str(tmp_path),
            "timeline": [],
            "sources": {},
            "benefit_overlays": {"mode": "disabled", "timing": {"scope": "full"}},
            "visual_dedup": {
                "enabled": True,
                "border_overlay": {"mode": "required", "selection_mode": "random"},
            },
            "output": {"directory": str(tmp_path / "out"), "width": 180, "height": 320},
        }
    )


def test_global_visual_border_library_creates_shared_layout_and_deduplicates(tmp_path):
    library = VisualBorderLibrary(tmp_path / "config")
    source = tmp_path / "边框.mov"
    generate_border(source)

    first = library.upload(source.name, source, 0)
    duplicate = library.upload("重命名.mov", source, first["revision"])

    assert (tmp_path / "config" / GLOBAL_BORDER_DIRECTORY).is_dir()
    assert (tmp_path / "config" / GLOBAL_BORDER_REGISTRY).is_file()
    assert first["revision"] == 1
    assert duplicate["deduplicated"] is True
    assert duplicate["revision"] == 1
    assert len(duplicate["assets"]) == 1
    stored = Path(first["asset"]["storage_path"])
    assert stored.parent == GLOBAL_BORDER_DIRECTORY
    assert stored.name.startswith(first["asset"]["content_hash"])
    assert (tmp_path / "config" / stored).is_file()


def test_global_visual_border_library_discovers_valid_files_copied_into_directory(
    tmp_path,
):
    library = VisualBorderLibrary(tmp_path / "config")
    initial = library.list()
    copied = Path(initial["directory"]) / "手工放入.mov"
    generate_border(copied)

    discovered = library.list()

    assert discovered["revision"] == 1
    assert len(discovered["assets"]) == 1
    assert discovered["assets"][0]["display_name"] == copied.name
    assert discovered["assets"][0]["storage_path"] == str(
        GLOBAL_BORDER_DIRECTORY / copied.name
    )
    assert library.list()["revision"] == 1


def test_global_visual_border_library_ignores_invalid_files_copied_into_directory(
    tmp_path,
):
    library = VisualBorderLibrary(tmp_path / "config")
    initial = library.list()
    (Path(initial["directory"]) / "无效.mov").write_bytes(b"not a movie")

    refreshed = library.list()

    assert refreshed["revision"] == 0
    assert refreshed["assets"] == []


def test_global_visual_border_library_uses_revision_and_project_compatibility(tmp_path):
    library = VisualBorderLibrary(tmp_path / "config")
    first_source = tmp_path / "红.mov"
    second_source = tmp_path / "绿.mov"
    generate_border(first_source, "red")
    generate_border(second_source, "green")
    first = library.upload(first_source.name, first_source, 0)

    with pytest.raises(VisualBorderLibraryConflict):
        library.upload(second_source.name, second_source, 0)

    second = library.upload(second_source.name, second_source, first["revision"])
    config = visual_config(tmp_path)
    assets = library.assets_for_config(config)
    scan = scan_config(config, assets)

    assert scan.ok
    assert len(scan.assets["visual_border"]) == 2
    assert all(asset.valid for asset in scan.assets["visual_border"])
    assert second["revision"] == 2

    config.output.width = 720
    incompatible = scan_config(config, library.assets_for_config(config))
    assert not incompatible.ok
    assert "不一致" in incompatible.errors[0]
