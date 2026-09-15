from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from smartstitch.config import ConfigStore
from smartstitch.models import AppConfig, ConfigUpdateRequest


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
