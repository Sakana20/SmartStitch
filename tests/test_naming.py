from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartstitch.models import AppConfig, Asset, MediaProbe, ScanResult, SourceMode
from smartstitch.naming import NamingError, parse_asset_naming
from smartstitch.planner import build_plan


def naming_config(tmp_path, *, categories=None) -> AppConfig:
    return AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "naming-test",
            "name": "命名测试",
            "source_root": str(tmp_path),
            "timeline": ["pool_2", "pool_1"],
            "sources": {
                "pool_1": {"label": "视频库 1", "directory": "pool_1"},
                "pool_2": {"label": "视频库 2", "directory": "pool_2"},
            },
            "benefit_overlays": {
                "mode": "disabled",
                "file": "",
                "timing": {"scope": "full"},
            },
            "output": {
                "directory": str(tmp_path / "output"),
                "naming": {
                    "enabled": True,
                    "product": "燕麦奶",
                    "benefit": "第二件半价",
                    "source_metadata": {"categories": categories or ["pool_*"]},
                },
            },
        }
    )


def naming_asset(category: str, filename: str) -> Asset:
    return Asset(
        id=f"{category}-{filename}",
        category=category,
        path=f"/{category}/{filename}",
        name=filename,
        media_type="video",
        probe=MediaProbe(duration=1, width=720, height=1280, fps=30),
    )


def test_generic_dynamic_naming_uses_timeline_order_earliest_date_and_sequence(tmp_path):
    config = naming_config(tmp_path)
    scan = ScanResult(
        config_id=config.id,
        assets={
            "pool_1": [
                naming_asset(
                    "pool_1",
                    "00016_张三-红果拿下了我全家-2026-10-31__001_pool-1_f0-10.mp4",
                )
            ],
            "pool_2": [
                naming_asset(
                    "pool_2",
                    "00018_李四-红果拿下了我全家-2026-10-20__002_pool-2_f10-20.mp4",
                )
            ],
            "benefit_overlay": [],
        },
    )

    plan = build_plan(config, scan, 2, seed=7)

    assert plan.items[0].output_name == "燕麦奶-第二件半价-李四+张三-20261020-1.mp4"
    assert plan.items[1].output_name == "燕麦奶-第二件半价-李四+张三-20261020-2.mp4"
    assert plan.items[0].naming is not None
    assert plan.items[0].naming.talents == ["李四", "张三"]
    assert plan.items[0].naming.restriction_date == "2026-10-20"
    assert plan.items[0].naming.sequence == 1
    assert plan.items[1].naming.sequence == 2
    assert [source.category for source in plan.items[0].naming.sources] == [
        "pool_2",
        "pool_1",
    ]


def test_explicit_naming_pools_exclude_other_stitched_pools(tmp_path):
    config = naming_config(tmp_path, categories=["pool_1"])
    scan = ScanResult(
        config_id=config.id,
        assets={
            "pool_1": [naming_asset("pool_1", "00016_张三-中间文案-2026-10-31.mp4")],
            "pool_2": [naming_asset("pool_2", "不需解析.mp4")],
            "benefit_overlay": [],
        },
    )

    item = build_plan(config, scan, 1, seed=3).items[0]

    assert item.output_name == "燕麦奶-第二件半价-张三-20261031-1.mp4"
    assert item.naming is not None
    assert [source.category for source in item.naming.sources] == ["pool_1"]


def test_disabled_naming_pool_omits_talent_and_date(tmp_path):
    config = naming_config(tmp_path, categories=["pool_1"])
    config.sources["pool_1"].mode = SourceMode.DISABLED
    scan = ScanResult(
        config_id=config.id,
        assets={
            "pool_1": [],
            "pool_2": [naming_asset("pool_2", "普通素材.mp4")],
            "benefit_overlay": [],
        },
    )

    item = build_plan(config, scan, 1, seed=3).items[0]

    assert item.output_name == "燕麦奶-第二件半价-1.mp4"
    assert item.naming.talents == []
    assert item.naming.restriction_date is None
    assert item.naming.sources == []


def test_sequence_can_start_from_configured_number(tmp_path):
    config = naming_config(tmp_path)
    config.output.naming.sequence_start = 50
    scan = ScanResult(
        config_id=config.id,
        assets={
            "pool_1": [naming_asset("pool_1", "00016_张三-中间文案-2026-10-31.mp4")],
            "pool_2": [naming_asset("pool_2", "00018_李四-中间文案-2026-10-20.mp4")],
            "benefit_overlay": [],
        },
    )

    plan = build_plan(config, scan, 2, seed=3)

    assert [item.naming.sequence for item in plan.items] == [50, 51]
    assert plan.items[0].output_name.endswith("-50.mp4")
    assert plan.items[1].output_name.endswith("-51.mp4")


def test_naming_rejects_invalid_date_and_non_generic_enablement(tmp_path):
    config = naming_config(tmp_path)
    with pytest.raises(NamingError, match="无效的限制日期"):
        parse_asset_naming(
            config,
            naming_asset("pool_1", "00016_张三-中间文案-2026-02-30.mp4"),
        )

    legacy = config.model_dump(mode="json")
    legacy.update(
        {
            "schema_version": 2,
            "workflow_type": "taobao_flash",
            "timeline": ["hook", "benefit_1", "ending"],
            "sources": {
                category: {"directory": category}
                for category in ["hook", "benefit_1", "ending"]
            },
        }
    )
    with pytest.raises(ValidationError, match="仅支持通用视频项目"):
        AppConfig.model_validate(legacy)


def test_naming_template_requires_all_business_fields(tmp_path):
    data = naming_config(tmp_path).model_dump(mode="json")
    data["output"]["naming"]["template"] = "{product}-{talents}.mp4"

    with pytest.raises(ValidationError, match="缺少必需变量"):
        AppConfig.model_validate(data)


@pytest.mark.parametrize(
    ("filename", "talent", "restriction_date"),
    [
        (
            "00016_小慧不大乖-红果拿下了我全家-2026-11-14__002_pool-8_f3-1327.mp4",
            "小慧不大乖",
            "2026-11-14",
        ),
        (
            "00018_磊金夫妇-红果拿下了我全家-2027-05-17__001_pool-8_f0-1391.mp4",
            "磊金夫妇",
            "2027-05-17",
        ),
    ],
)
def test_pool_8_real_filename_examples(tmp_path, filename, talent, restriction_date):
    metadata = parse_asset_naming(
        naming_config(tmp_path),
        naming_asset("pool_8", filename),
    )

    assert metadata.talent == talent
    assert metadata.restriction_date == restriction_date
