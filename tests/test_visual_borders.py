from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from smartstitch.models import AppConfig
from smartstitch.scanner import scan_config
from smartstitch.visual_borders import (
    GLOBAL_BORDER_DIRECTORY,
    GLOBAL_BORDER_REGISTRY,
    GLOBAL_EFFECT_REGISTRY,
    LEGACY_GLOBAL_BORDER_DIRECTORY,
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
                "background": {"enabled": False},
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


def test_global_visual_border_library_removes_records_for_deleted_files(tmp_path):
    library = VisualBorderLibrary(tmp_path / "config")
    source = tmp_path / "待删除.mov"
    generate_border(source)
    uploaded = library.upload(source.name, source, 0)
    stored = tmp_path / "config" / uploaded["asset"]["storage_path"]
    stored.unlink()

    refreshed = library.list()

    assert refreshed["revision"] == 2
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


def test_global_visual_effect_libraries_can_be_created_uploaded_and_deleted(tmp_path):
    library = VisualBorderLibrary(tmp_path / "config")
    initial = library.list_effect_libraries()

    created = library.create_effect_library("烟花", expected_revision=initial["revision"])
    library_id = created["library"]["library_id"]
    assert library_id == "effect_2"
    source = tmp_path / "烟花.mov"
    generate_border(source, "blue")
    uploaded = library.upload_effect_asset(
        library_id,
        source.name,
        source,
        expected_revision=created["revision"],
    )

    fireworks = next(
        item for item in uploaded["libraries"] if item["library_id"] == library_id
    )
    assert fireworks["name"] == "烟花"
    assert len(fireworks["assets"]) == 1
    assert Path(fireworks["assets"][0]["storage_path"]).parts[-2] == library_id

    deleted = library.delete_effect_library(
        library_id,
        expected_revision=uploaded["revision"],
    )
    assert all(item["library_id"] != library_id for item in deleted["libraries"])


def test_legacy_visual_effect_directories_migrate_to_stable_numbered_libraries(
    tmp_path,
):
    root = tmp_path / "config"
    library = VisualBorderLibrary(root)
    border_source = tmp_path / "透明边框.mov"
    generate_border(border_source, "red")
    uploaded = library.upload(border_source.name, border_source, 0)

    legacy_directory = root / LEGACY_GLOBAL_BORDER_DIRECTORY
    legacy_directory.parent.mkdir(parents=True, exist_ok=True)
    (root / GLOBAL_BORDER_DIRECTORY).replace(legacy_directory)
    border_registry = json.loads(
        (root / GLOBAL_BORDER_REGISTRY).read_text(encoding="utf-8")
    )
    border_registry["assets"][0]["storage_path"] = str(
        LEGACY_GLOBAL_BORDER_DIRECTORY
        / Path(uploaded["asset"]["storage_path"]).name
    )
    (root / GLOBAL_BORDER_REGISTRY).write_text(
        json.dumps(border_registry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    effect_registry = json.loads(
        (root / GLOBAL_EFFECT_REGISTRY).read_text(encoding="utf-8")
    )
    effect_registry["schema_version"] = 1
    effect_registry.pop("next_library_number", None)
    effect_registry["libraries"][0]["library_id"] = "visual-border"
    (root / GLOBAL_EFFECT_REGISTRY).write_text(
        json.dumps(effect_registry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    flat_effect = root / "全局素材库" / "视觉特效" / "节庆烟花.mov"
    flat_effect.parent.mkdir(parents=True, exist_ok=True)
    generate_border(flat_effect, "yellow")

    migrated = VisualBorderLibrary(root).list_effect_libraries()

    assert [item["library_id"] for item in migrated["libraries"]] == [
        "effect_1",
        "effect_2",
    ]
    border = migrated["libraries"][0]
    current_effects = migrated["libraries"][1]
    assert border["name"] == "透明边框"
    assert border["assets"][0]["display_name"] == border_source.name
    assert current_effects["name"] == "视觉特效"
    assert current_effects["assets"][0]["display_name"] == flat_effect.name
    assert not legacy_directory.exists()
    assert Path(border["directory"]).name == "effect_1"
    assert Path(current_effects["directory"]).name == "effect_2"

    renamed = VisualBorderLibrary(root).update_effect_library(
        "effect_2",
        expected_revision=migrated["revision"],
        updates={"name": "节日动效"},
    )
    renamed_effect = next(
        item for item in renamed["libraries"] if item["library_id"] == "effect_2"
    )
    assert renamed_effect["name"] == "节日动效"
    assert Path(renamed_effect["directory"]).name == "effect_2"

    created = VisualBorderLibrary(root).create_effect_library(
        "光效", expected_revision=renamed["revision"]
    )
    assert created["library"]["library_id"] == "effect_3"
