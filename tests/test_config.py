from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from smartstitch.config import ConfigStore
from smartstitch.models import (
    AppConfig,
    ConfigUpdateRequest,
    LibraryPreflightRequest,
    PreviewRequest,
    SliceAssignment,
    TimelineAnalyzeRequest,
)


def config_data(tmp_path) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "id": "config-test",
        "name": "配置测试",
        "source_root": str(tmp_path),
        "timeline": ["hook", "benefit_1", "ending"],
        "sources": {
            category: {"mode": "required", "directory": category}
            for category in ["hook", "benefit_1", "ending"]
        },
        "benefit_overlays": {"mode": "disabled", "file": ""},
        "output": {"directory": str(tmp_path / "output")},
    }


def test_generic_schema_accepts_empty_project_and_dynamic_pool_names(tmp_path):
    empty = AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "generic-test",
            "name": "通用配置",
            "source_root": str(tmp_path),
            "timeline": [],
            "sources": {},
            "benefit_overlays": {
                "mode": "disabled",
                "file": "",
                "timing": {"scope": "full"},
            },
            "output": {"directory": str(tmp_path / "output")},
        }
    )
    assert empty.workflow_type == "generic"

    data = empty.model_dump(mode="json")
    data["timeline"] = ["pool_2", "pool_1"]
    data["sources"] = {
        "pool_1": {"label": "开场", "directory": "视频库/pool_1"},
        "pool_2": {"label": "展示", "directory": "视频库/pool_2"},
    }
    config = AppConfig.model_validate(data)
    assert config.timeline == ["pool_2", "pool_1"]
    assert config.sources["pool_1"].label == "开场"

    data["sources"]["custom"] = {"directory": "bad"}
    data["timeline"].append("custom")
    with pytest.raises(ValidationError, match="pool_<正整数>"):
        AppConfig.model_validate(data)


def test_path_fields_remove_macos_copy_quotes_without_changing_filename(tmp_path):
    copied_path = (
        "'/Volumes/Elements SE/陈鼎琦/饿了么整理/素材/星广一口价二剪/原始视频/"
        "26 室友要喝我的奶茶 #剧情演绎 #淘宝闪购 #外卖.mp4'"
    )
    expected = copied_path[1:-1]

    assert TimelineAnalyzeRequest(source_path=copied_path).source_path == expected
    preview = PreviewRequest(
        config_id="config-test", count=1, output_directory=f'"{tmp_path}"'
    )
    library = LibraryPreflightRequest(
        parent_directory=f"‘{tmp_path}’", folder_name="视频库"
    )
    assert preview.output_directory == str(tmp_path)
    assert library.parent_directory == str(tmp_path)

    data = config_data(tmp_path)
    data["source_root"] = copied_path
    data["sources"]["hook"]["directory"] = f'"{tmp_path / "hook"}"'
    data["benefit_overlays"] = {
        "mode": "optional",
        "file": f"“{tmp_path / '提示语.png'}”",
    }
    data["output"]["directory"] = f"'{tmp_path / 'output'}'"
    config = AppConfig.model_validate(data)

    assert config.source_root == expected
    assert config.sources["hook"].directory == str(tmp_path / "hook")
    assert config.benefit_overlays.file == str(tmp_path / "提示语.png")
    assert config.output.directory == str(tmp_path / "output")


def test_slice_assignment_migrates_single_index_and_validates_groups():
    legacy = SliceAssignment.model_validate(
        {"segment_index": 3, "category": "benefit_1"}
    )
    assert legacy.segment_indexes == [3]

    grouped = SliceAssignment.model_validate(
        {
            "client_unit_id": "unit-1",
            "segment_indexes": [5, 2],
            "category": "hook",
        }
    )
    assert grouped.segment_indexes == [5, 2]

    with pytest.raises(ValidationError, match="不能同时提交"):
        SliceAssignment.model_validate(
            {
                "segment_index": 1,
                "segment_indexes": [1],
                "category": "hook",
            }
        )
    with pytest.raises(ValidationError, match="不能重复"):
        SliceAssignment.model_validate(
            {"segment_indexes": [1, 1], "category": "hook"}
        )


def test_schema_one_migrates_in_memory_without_mutating_source(tmp_path):
    legacy = config_data(tmp_path)
    legacy["schema_version"] = 1
    legacy["timeline"] = ["hook", "benefit_video", "ending"]
    legacy["sources"]["benefit_video"] = legacy["sources"].pop("benefit_1")
    legacy["benefit_overlays"] = {
        "mode": "disabled",
        "timing": {"scope": "benefit_video"},
    }
    original = copy.deepcopy(legacy)

    config = AppConfig.model_validate(legacy)

    assert legacy == original
    assert config.schema_version == 2
    assert config.timeline == ["hook", "benefit_1", "ending"]
    assert config.benefit_categories() == ["benefit_1"]
    assert config.benefit_overlays.timing.scope == "benefits"


def test_loading_legacy_file_does_not_rewrite_until_explicit_save(tmp_path):
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    legacy = config_data(tmp_path)
    legacy["schema_version"] = 1
    legacy["timeline"] = ["hook", "benefit_video", "ending"]
    legacy["sources"]["benefit_video"] = legacy["sources"].pop("benefit_1")
    path = config_directory / "config-test.yaml"
    original_text = yaml.safe_dump(legacy, allow_unicode=True, sort_keys=False)
    path.write_text(original_text, encoding="utf-8")
    store = ConfigStore(config_directory)

    assert store.load("config-test").schema_version == 2
    assert path.read_text("utf-8") == original_text

    store.save_text("config-test", ConfigUpdateRequest(yaml_text=original_text))
    saved = yaml.safe_load(path.read_text("utf-8"))
    assert saved["schema_version"] == 2
    assert saved["timeline"] == ["hook", "benefit_1", "ending"]
    assert "benefit_video" not in saved["sources"]


def test_shared_config_paths_follow_mount_alias_without_rewriting_yaml(tmp_path):
    canonical = tmp_path / "Volumes" / "home" / "Smartstitch"
    mounted = tmp_path / "Volumes" / "homes" / "nas-user" / "Smartstitch"
    mounted.mkdir(parents=True)
    library_root = canonical / "共享视频库"
    data = config_data(library_root)
    data["sources"]["hook"]["directory"] = str(
        library_root / "切片素材" / "引子"
    )
    data["sources"]["hook"]["items"] = [
        {"path": str(library_root / "切片素材" / "引子" / "素材.mp4")}
    ]
    data["benefit_overlays"] = {
        "mode": "optional",
        "file": str(library_root / "风险提示语图片" / "提示语.png"),
    }
    data["output"]["directory"] = str(library_root / "成片输出")
    path = mounted / "config-test.yaml"
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    store = ConfigStore(mounted, canonical_directory=canonical)

    config = store.load("config-test")
    mounted_library = mounted / "共享视频库"
    assert config.source_root == str(mounted_library)
    assert config.sources["hook"].directory == str(
        mounted_library / "切片素材" / "引子"
    )
    assert config.sources["hook"].items[0].path == str(
        mounted_library / "切片素材" / "引子" / "素材.mp4"
    )
    assert config.benefit_overlays.file == str(
        mounted_library / "风险提示语图片" / "提示语.png"
    )
    assert config.output.directory == str(mounted_library / "成片输出")

    config.name = "远端电脑修改"
    store.save_config("config-test", config)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["name"] == "远端电脑修改"
    assert saved["source_root"] == str(library_root)
    assert saved["sources"]["hook"]["directory"] == str(
        library_root / "切片素材" / "引子"
    )
    assert saved["benefit_overlays"]["file"] == str(
        library_root / "风险提示语图片" / "提示语.png"
    )
    assert saved["output"]["directory"] == str(library_root / "成片输出")


def test_benefit_segments_allow_gaps_and_follow_timeline_order(tmp_path):
    data = config_data(tmp_path)
    data["timeline"] = ["hook", "benefit_3", "benefit_1", "ending"]
    data["sources"]["benefit_3"] = {
        "mode": "required",
        "directory": "benefit_3",
    }

    config = AppConfig.model_validate(data)

    assert config.benefit_categories() == ["benefit_3", "benefit_1"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data.update(
                timeline=["hook", "ending"],
                sources={
                    key: value
                    for key, value in data["sources"].items()
                    if key != "benefit_1"
                },
            ),
            "至少需要一个",
        ),
        (
            lambda data: data.update(
                timeline=["hook", "benefit_1", "benefit_1", "ending"]
            ),
            "不得重复",
        ),
        (
            lambda data: (
                data.update(timeline=["hook", "benefit_people", "ending"]),
                data["sources"].update(
                    {"benefit_people": {"mode": "required", "directory": "bad"}}
                ),
            ),
            "benefit_<正整数>",
        ),
    ],
)
def test_invalid_benefit_segment_structures_are_rejected(tmp_path, mutate, message):
    data = config_data(tmp_path)
    mutate(data)

    with pytest.raises(ValidationError, match=message):
        AppConfig.model_validate(data)


def test_more_than_twenty_benefit_segments_is_rejected(tmp_path):
    data = config_data(tmp_path)
    benefits = [f"benefit_{index}" for index in range(1, 22)]
    data["timeline"] = ["hook", *benefits, "ending"]
    data["sources"].update(
        {
            category: {"mode": "required", "directory": category}
            for category in benefits
        }
    )

    with pytest.raises(ValidationError, match="最多 20 个"):
        AppConfig.model_validate(data)
